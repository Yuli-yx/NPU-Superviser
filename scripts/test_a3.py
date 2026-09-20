"""A3 双 die 解析与采集事务回归；只使用内存数据库。"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector
import db


SAMPLE = """| npu-smi 25.5.1 | Version: 25.5.1 |
| Chip Phy-ID | Bus-Id | AICore(%) Memory-Usage(MB) HBM-Usage(MB) |
| 0 Ascend910 | OK | 164.9 49 0 / 0 |
| 0 0 | 0000:9D:00.0 | 12 0 / 0 61839 / 65536 |
| 0 Ascend910 | Warning | - 48 0 / 0 |
| 1 1 | 0000:9F:00.0 | 90 0 / 0 61528 / 65536 |
| 1 Ascend910 | OK | 168.0 52 0 / 0 |
| 0 2 | 0000:99:00.0 | 0 0 / 0 61777 / 65536 |
| 1 Ascend910 | OK | - 53 0 / 0 |
| 1 3 | 0000:9B:00.0 | 1 0 / 0 61529 / 65536 |
"""


class A3Test(unittest.TestCase):
    def test_dual_die(self):
        cards = collector.parse_npu_smi(SAMPLE)["cards"]
        self.assertEqual([c["npu_id"] for c in cards], [0, 1, 2, 3])
        self.assertEqual([c["hbm_used_mb"] for c in cards], [61839, 61528, 61777, 61529])
        self.assertTrue(all(c["hbm_total_mb"] == 65536 for c in cards))
        self.assertEqual([c["aicore_pct"] for c in cards], [12, 90, 0, 1])
        self.assertEqual(cards[1]["health"], "Warning")
        self.assertEqual(cards[0]["power_w"], 164.9)
        self.assertEqual(cards[0]["temp_c"], 49)
        self.assertIsNone(cards[1]["power_w"])
        self.assertEqual(cards[1]["temp_c"], 48)
        self.assertEqual(cards[3]["temp_c"], 53)

    def test_separate_phy_id_column(self):
        sample = """| Chip | Phy-ID | Bus-Id | AICore(%) HBM-Usage(MB) |
| 0 | Ascend910 | OK | 100 40 0 / 0 |
| 0 | 6 | 0000:9D:00.0 | 12 1024 / 65536 |
| 0 | Ascend910 | OK | - 41 0 / 0 |
| 1 | 7 | 0000:9F:00.0 | 90 2048 / 65536 |
"""
        cards = collector.parse_npu_smi(sample)["cards"]
        self.assertEqual([c["npu_id"] for c in cards], [6, 7])

    def test_invalid_phy_id(self):
        result = collector.parse_npu_smi(SAMPLE.replace("| 1 1 |", "| 1 NA |"))
        self.assertTrue(result["parse_error"])

    def test_transaction(self):
        conn = sqlite3.connect(":memory:")
        previous = getattr(db._local, "conn", None)
        db._local.conn = conn
        try:
            conn.executescript(db.SCHEMA)
            conn.execute("INSERT INTO server(id,name,ip,created_at,updated_at) VALUES(1,'test','test',0,0)")
            conn.commit()
            cards = collector.parse_npu_smi(SAMPLE)["cards"]
            collector.persist_result(1, {"ok": True, "cards": cards})
            before = conn.execute("SELECT * FROM server").fetchall()
            snapshot = conn.execute("SELECT * FROM card").fetchall()
            with self.assertRaises(sqlite3.IntegrityError):
                collector.persist_result(1, {"ok": True, "version": "bad", "cards": cards + cards})
            self.assertEqual(conn.execute("SELECT * FROM server").fetchall(), before)
            self.assertEqual(conn.execute("SELECT * FROM card").fetchall(), snapshot)
            collector.persist_result(1, {"ok": False, "error": "timeout"})
            self.assertEqual(conn.execute("SELECT status,last_error FROM server").fetchone(), ("offline", "timeout"))
            self.assertEqual(conn.execute("SELECT * FROM card").fetchall(), snapshot)
        finally:
            conn.close()
            db._local.conn = previous


if __name__ == "__main__":
    unittest.main()
