"""台账升级、CSV 和排序回归；不读写实际 data.db。"""
import io
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import db
import ledger_csv


class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.previous = getattr(db._local, "conn", None)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db._local.conn = self.conn
        db.init_db()
        self.client = app.app.test_client()

    def tearDown(self):
        self.conn.close()
        db._local.conn = self.previous

    def upload(self, text):
        content = text.encode("utf-8-sig") if isinstance(text, str) else text
        return self.client.post("/api/ledger/import", data={"file": (io.BytesIO(content), "test.csv")})

    def test_old_schema_migration(self):
        self.conn.execute("DROP TABLE server")
        self.conn.executescript(db.SCHEMA.replace("  network_groups  TEXT NOT NULL DEFAULT '{}',      -- 各网络独立互通组，仅为人工标记\n", "").replace("  sort_order      INTEGER NOT NULL DEFAULT 0,\n", ""))
        self.conn.execute("INSERT INTO server(id,name,ip,created_at,updated_at) VALUES(9,'old','old',0,0)")
        self.conn.commit()
        db.init_db()
        server = db.get_server(9)
        self.assertEqual(server["network_groups"], {})
        self.assertEqual(server["sort_order"], 9)
        db.init_db()
        self.assertEqual(len(db.list_servers()), 1)

    def test_minimum_import_and_defaults(self):
        res = self.upload("IP（必填）\n192.0.2.1\n")
        self.assertEqual(res.status_code, 200, res.json)
        server = db.list_servers()[0]
        self.assertEqual(server["name"], "192.0.2.1")
        self.assertEqual(server["username"], "root")
        self.assertEqual(server["ssh_port"], 22)
        self.assertTrue(server["collect_enabled"])
        self.assertEqual(db.get_config("interval_seconds"), "1800")

    def test_roundtrip_and_blank_preservation(self):
        original = db.create_server({"name": "test", "ip": "192.0.2.1", "password": "=secret,\nquoted", "model": "A3",
                                     "tags": ["RoCE", "UBG"], "network_groups": {"roce": "A", "ubg": "B"}})
        content = self.client.get("/api/ledger.csv").data
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"'=secret", content)
        res = self.upload(content)
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(res.json, {"added": 0, "updated": 1})
        server = db.get_server(original["id"])
        for key in ("password", "tags", "network_groups", "model"):
            self.assertEqual(server[key], original[key])
        res = self.upload("IP,名称,SSH密码,UBoE互通组,RoCE互通组\n192.0.2.2,test,,C,\n")
        self.assertEqual(res.status_code, 200)
        server = db.get_server(original["id"])
        self.assertEqual(server["password"], original["password"])
        self.assertEqual(server["network_groups"], {"uboe": "C", "roce": "A", "ubg": "B"})

    def test_invalid_file_is_atomic(self):
        for text in ("IP,SSH端口\n192.0.2.1,22\n192.0.2.2,0\n",
                     "IP,名称\n192.0.2.1,a\n192.0.2.2,a\n",
                     "IP,未知列\n192.0.2.1,a\n",
                     "IP,名称\n192.0.2.1\n"):
            res = self.upload(text)
            self.assertEqual(res.status_code, 400, res.json)
            self.assertEqual(db.list_servers(), [])

    def test_encoding_and_template(self):
        res = self.upload("IP,名称\n192.0.2.1,测试服务器\n".encode("gb18030"))
        self.assertEqual(res.status_code, 200)
        content = self.client.get("/api/ledger.csv?template=1").data
        self.assertEqual(len(content.decode("utf-8-sig").splitlines()), 1)
        self.assertEqual(self.upload(content).status_code, 400)

    def test_formula_escape_is_reversible(self):
        for value in ("=1+1", "  =1+1", "+cmd", "-cmd", "@SUM(A1)", "'=text", "\ttext", "normal"):
            self.assertEqual(ledger_csv.unprotect(ledger_csv.protect(value)), value)
        self.assertEqual(ledger_csv.protect("  =1+1"), "'  =1+1")

    def test_template_options_and_model_validation(self):
        db.set_config({"models_json": '[{"name":"A3","cards":16},{"name":"A5-custom(950DT)","cards":8}]'})
        db.create_server({"name": "existing", "ip": "existing", "network_groups": {"uboe": "126-129"}})
        headers = self.client.get("/api/ledger.csv?template=1").data.decode("utf-8-sig")
        self.assertIn("A5-custom(950DT)", headers)
        self.assertIn("可选：是 / 否", headers)
        self.assertIn("已有组：126-129", headers)
        content = headers + '192.0.2.1,new," a5-CUSTOM （950dt） ",,,,,,,,,,\r\n'
        res = self.upload(content)
        self.assertEqual(res.status_code, 200, res.json)
        self.assertEqual(db.get_server_by_name("new")["model"], "A5-custom(950DT)")
        res = self.upload("IP,型号\n192.0.2.2,A5\n")
        self.assertEqual(res.status_code, 400, res.json)
        self.assertIsNone(db.get_server_by_name("192.0.2.2"))
        # 旧模板/列名兼容，网络标签大小写归一而自由标签仍保留。
        res = self.upload("IP,型号,组网标签\n192.0.2.3,a3,\"roce,RoCE,UBOE,训练组\"\n")
        self.assertEqual(res.status_code, 200, res.json)
        server = db.get_server_by_name("192.0.2.3")
        self.assertEqual(server["model"], "A3")
        self.assertEqual(server["tags"], ["RoCE", "UBoE", "训练组"])

    def test_networks_and_order(self):
        a = db.create_server({"name": "a", "ip": "a", "network_groups": {"uboe": "126-129"}})
        b = db.create_server({"name": "b", "ip": "b", "network_groups": {"uboe": "other"}})
        self.assertNotEqual(a["network_groups"], b["network_groups"])
        response = self.client.put("/api/servers/order", json={"ids": [b["id"], a["id"]]})
        self.assertEqual(response.status_code, 200, response.json)
        db.init_db()
        self.assertEqual([s["id"] for s in db.list_servers()], [b["id"], a["id"]])
        for ids in ([a["id"], a["id"]], [a["id"]], [True, b["id"]]):
            response = self.client.put("/api/servers/order", json={"ids": ids})
            self.assertIn(response.status_code, (400, 409))
            self.assertEqual([s["id"] for s in db.list_servers()], [b["id"], a["id"]])
        response = self.client.patch(f'/api/servers/{a["id"]}', json={"network_groups": {"bad": "A"}})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
