"""隔离浏览器验收服务：仅回环地址、临时假数据、绝不 SSH 到真实机器。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import collector
import db
from waitress import serve


def fake_collect(server, **kwargs):
    if server["name"].startswith("offline"):
        return {"ok": False, "error": "test network unavailable"}
    count = 16 if server["model"] == "A3" else 8
    return {"ok": True, "cards": [{"npu_id": i, "chip_name": "TestChip", "health": "OK",
                                   "aicore_pct": 30 if i == 0 else 0, "hbm_used_mb": 0,
                                   "hbm_total_mb": 65536} for i in range(count)], "version": "test"}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="npu-web-test-") as directory:
        db.DB_PATH = Path(directory) / "test.db"
        db.init_db()
        collector.collect_one = fake_collect
        for name, model, group in (("test-a5-one", "A5-850(950PR)", "pair"),
                                   ("test-a5-two", "A5-850(950PR)", "pair"),
                                   ("test-a5-isolated", "A5-850E(950DT)", "other"),
                                   ("test-a3-one", "A3", "")):
            s = db.create_server({"name": name, "model": model, "ip": "192.0.2.1", "password": "TEST-only-password",
                                  "network_groups": {"uboe": group} if group else {}, "expected_cards": 3})
            collector.persist_result(s["id"], fake_collect(s))
        collector.collect_server_now(1)
        db.occupy(1, 1, "test-user", "test-purpose")
        print("ISOLATED fake-data server http://127.0.0.1:8667", flush=True)
        serve(app.app, host="127.0.0.1", port=8667, threads=4)
