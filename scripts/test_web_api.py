"""网页相关 API、限流和采集互斥回归，仅使用内存库/Mock。"""
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import collector
import db
from request_limits import RequestLimiter


class WebApiTest(unittest.TestCase):
    def setUp(self):
        self.previous = getattr(db._local, "conn", None)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        db._local.conn = self.conn
        db.init_db()
        self.enabled = app.app.config["RATE_LIMIT_ENABLED"]
        app.app.config["RATE_LIMIT_ENABLED"] = False
        app.request_limiter.reset()
        self.client = app.app.test_client()
        self.s = db.create_server({"name": "test", "ip": "192.0.2.1", "model": "A3", "password": "TEST-only"})
        self.sid = self.s["id"]
        self.cards = [{"npu_id": i, "health": "OK", "aicore_pct": 0, "hbm_used_mb": 0, "hbm_total_mb": 65536} for i in range(16)]
        collector.persist_result(self.sid, {"ok": True, "cards": self.cards})

    def tearDown(self):
        self.conn.close()
        db._local.conn = self.previous
        app.app.config["RATE_LIMIT_ENABLED"] = self.enabled
        app.request_limiter.reset()

    def test_crud_and_cascade(self):
        r = self.client.post("/api/servers", json={"name": "new", "ip": "192.0.2.2", "collect_enabled": False})
        self.assertEqual(r.status_code, 201, r.json)
        sid = r.json["id"]
        r = self.client.put(f"/api/servers/{sid}", json={"name": "renamed", "ip": "192.0.2.3", "password": "TEST-update"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db.get_server(sid)["password"], "TEST-update")
        self.assertEqual(self.client.post("/api/servers", json={"name": "renamed", "ip": "duplicate"}).status_code, 409)
        self.assertEqual(self.client.put(f"/api/servers/{sid}", json={"name": "test", "ip": "duplicate"}).status_code, 409)
        db.occupy(self.sid, 1, "tester", "")
        self.assertEqual(self.client.delete(f"/api/servers/{self.sid}").status_code, 200)
        self.assertEqual(len(db.active_occupancies()), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM card").fetchone()[0], 0)

    def test_occupy_release_and_invalid_card(self):
        body = {"server_id": self.sid, "npu_id": 15, "user": "tester", "purpose": "test"}
        r = self.client.post("/api/occupancy", json=body)
        self.assertEqual(r.status_code, 201, r.json)
        occ = r.json["id"]
        self.assertEqual(self.client.post("/api/occupancy", json=body).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/occupancy/{occ}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/occupancy/{occ}").status_code, 404)
        self.assertEqual(self.client.post("/api/occupancy", json={**body, "npu_id": 99}).status_code, 400)
        self.assertEqual(self.client.post("/api/occupancy", json={**body, "server_id": 999}).status_code, 404)
        self.assertEqual(self.client.post("/api/occupancy", json={**body, "user": ""}).status_code, 400)

    def test_collect_success_failure_and_disabled(self):
        db.update_server(self.sid, {"collect_enabled": False})
        with patch.object(collector, "collect_one", return_value={"ok": True, "cards": self.cards}):
            r = self.client.post(f"/api/servers/{self.sid}/collect")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json["cards"], 16)
        with patch.object(collector, "collect_one", return_value={"ok": False, "error": "Timed out"}):
            self.assertEqual(self.client.post(f"/api/servers/{self.sid}/collect").status_code, 502)
        self.assertEqual(db.get_server(self.sid)["status"], "offline")
        self.assertEqual(len(db.build_dashboard()["servers"][0]["cards"]), 16)
        with patch.object(collector, "collect_server_now", side_effect=collector.CollectionInProgress("busy")):
            self.assertEqual(self.client.post(f"/api/servers/{self.sid}/collect").status_code, 409)

    def test_bad_input_and_settings_atomicity(self):
        for payload in ([], "bad", 1):
            self.assertEqual(self.client.post("/api/servers", json=payload).status_code, 400)
        for payload in ({"name": 1, "ip": "x"}, {"name": "x", "ip": "x", "ssh_port": 0},
                        {"name": "x", "ip": "x", "tags": [1]}):
            self.assertEqual(self.client.post("/api/servers", json=payload).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/servers/{self.sid}", json={"collect_enabled": "false"}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/servers/{self.sid}", json={"ssh_port": 70000}).status_code, 400)
        before = db.get_all_config()
        for payload in ({"config": {"collect_workers": 0}}, {"config": {"interval_seconds": "bad"}},
                        {"models": [{"name": "A3", "cards": 16}, {"name": "a 3", "cards": 16}]},
                        {"models": {}}, {"models": [{"name": "A3", "cards": 65}]}):
            self.assertEqual(self.client.put("/api/config", json=payload).status_code, 400)
            self.assertEqual(db.get_all_config(), before)

    def test_dashboard_statuses_and_json_roundtrip(self):
        db.occupy(self.sid, 15, "tester", "test")
        dashboard = self.client.get("/api/dashboard").json
        self.assertEqual(dashboard["summary"]["cards_total"], 16)
        self.assertEqual(dashboard["summary"]["occupied"], 1)
        exported = self.client.get("/api/export").json
        imported = self.client.post("/api/import", json=exported)
        self.assertEqual(imported.status_code, 200, imported.json)
        self.assertEqual(len(db.list_servers()), 1)
        self.assertEqual(len(db.active_occupancies()), 1)
        for path in ("/", "/static/app.js", "/static/style.css", "/api/config", "/api/ledger.csv?template=1"):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)

    def test_http_limits_and_untrusted_forwarded_header(self):
        app.app.config["RATE_LIMIT_ENABLED"] = True
        for _ in range(5):
            self.assertEqual(self.client.get("/api/dashboard").status_code, 200)
        r = self.client.get("/api/dashboard", headers={"X-Forwarded-For": "new-client"})
        self.assertEqual(r.status_code, 429)
        self.assertGreater(int(r.headers["Retry-After"]), 0)
        self.assertEqual(self.client.get("/api/dashboard", environ_overrides={"REMOTE_ADDR": "192.0.2.2"}).status_code, 200)
        with patch.object(collector, "collect_server_now", return_value={"ok": True, "cards": self.cards}):
            self.assertEqual(self.client.post(f"/api/servers/{self.sid}/collect").status_code, 200)
            self.assertEqual(self.client.post(f"/api/servers/{self.sid}/collect", environ_overrides={"REMOTE_ADDR": "192.0.2.2"}).status_code, 429)
        for _ in range(5):
            with self.client.get("/") as response:
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 429)

    def test_worker_count_hot_reload(self):
        from concurrent.futures import ThreadPoolExecutor
        instance = collector.Collector()
        instance._workers = 1
        instance._pool = ThreadPoolExecutor(max_workers=1)
        db.set_config({"collect_workers": "3"})
        try:
            with patch.object(collector, "collect_server_now", return_value={"ok": True}) as call:
                instance.run_round()
                self.assertEqual(instance._workers, 3)
                call.assert_called_once_with(self.sid)
        finally:
            instance._pool.shutdown(wait=True)


class LimiterTest(unittest.TestCase):
    def test_window_recovery_and_bounds(self):
        now = [0]
        limiter = RequestLimiter(clock=lambda: now[0], max_keys=5)
        policy = [("a", 1, 30)]
        self.assertEqual(limiter.check(policy), 0)
        now[0] = 10
        self.assertEqual(limiter.check(policy), 20)
        now[0] = 30
        self.assertEqual(limiter.check(policy), 0)
        for i in range(20):
            limiter.check([(str(i), 1, 60)])
        self.assertLessEqual(len(limiter.events), 5)

    def test_concurrent_admission(self):
        limiter = RequestLimiter()
        results = []
        threads = [threading.Thread(target=lambda: results.append(limiter.check([("same", 1, 30)]))) for _ in range(16)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(results.count(0), 1)

    def test_collect_mutex(self):
        entered = threading.Event()
        release = threading.Event()
        failures = []
        def fake(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {"ok": True, "cards": []}
        def run():
            try: collector.collect_server_now(98765)
            except Exception as exc: failures.append(exc)
        with patch.object(db, "get_server", return_value={"id": 98765}), patch.object(db, "get_config_int", return_value=1), \
             patch.object(collector, "collect_one", side_effect=fake), patch.object(collector, "persist_result", side_effect=lambda sid, r: r):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(2))
            try:
                with self.assertRaises(collector.CollectionInProgress): collector.collect_server_now(98765)
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(failures)
            self.assertTrue(collector.collect_server_now(98765)["ok"])


if __name__ == "__main__":
    unittest.main()
