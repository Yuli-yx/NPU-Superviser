"""演示数据:不接真实服务器也能预览看板效果。

用法(项目根目录):
  python scripts/demo_seed.py           # 写入 3 台演示服务器(已存在则跳过)
  python scripts/demo_seed.py --clean   # 删除全部演示数据

演示服务器 collect_enabled=0(不参与定时采集),卡数据为静态快照。
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402

DEMO_MARK = "__demo__"

DEMOS = [
    dict(name="demo-atlas-a3-01", model="A3", ip="10.24.1.11", username="root",
         password="Demo@A3#01", tags=["RoCE", "参数面", "训练组"], cards=16,
         chip="910_93C0", hbm_total=131072),
    dict(name="demo-atlas-850e-07", model="A5-850E(950DT)", ip="10.24.2.7", username="HwHiAiUser",
         password="Demo@850E#7", tags=["RoCE", "推理组"], cards=8,
         chip="910B4", hbm_total=65536),
    dict(name="demo-pc16-03", model="A5-PC16(950DT)", ip="10.24.3.3", username="root",
         password="Demo@PC16#3", tags=["IB", "双平面"], cards=16,
         chip="910B4", hbm_total=65536),
]

USERS = [("张三", "llama3-70B 微调"), ("李四", "Qwen2.5 推理压测"), ("王五", "CANN 算子适配")]


def seed():
    db.init_db()
    now = int(__import__("time").time())
    for spec in DEMOS:
        if db.get_server_by_name(spec["name"]):
            print(f"跳过(已存在): {spec['name']}")
            continue
        server = db.create_server({
            "name": spec["name"], "model": spec["model"], "ip": spec["ip"],
            "ssh_port": 22, "username": spec["username"], "password": spec["password"],
            "note": DEMO_MARK, "tags": spec["tags"], "expected_cards": spec["cards"],
            "collect_enabled": False,
        })
        cards = []
        rng = random.Random(spec["name"])
        for i in range(spec["cards"]):
            busy = rng.random() < 0.35
            cards.append({
                "npu_id": i, "chip_name": spec["chip"], "health": "OK",
                "aicore_pct": round(rng.uniform(55, 98), 1) if busy else 0,
                "hbm_used_mb": int(spec["hbm_total"] * rng.uniform(.3, .8)) if busy
                               else rng.choice([0, 0, 512]),
                "hbm_total_mb": spec["hbm_total"],
                "power_w": round(rng.uniform(180, 397), 1) if busy else rng.uniform(65, 90),
                "temp_c": round(rng.uniform(48, 71) if busy else rng.uniform(38, 45), 1),
            })
        db.replace_cards(server["id"], cards, now)
        db.set_collect_result(server["id"], True, None, "24.1.0")
        # 登记几条占用
        occupied = rng.sample(range(spec["cards"]), k=min(3, spec["cards"]))
        for k, npu in enumerate(occupied):
            user, purpose = USERS[k % len(USERS)]
            try:
                db.occupy(server["id"], npu, user, purpose,
                          now - rng.randint(1800, 86400))
            except db.OccupiedError:
                pass
        print(f"已写入: {spec['name']}({spec['cards']} 卡)")


def clean():
    db.init_db()
    for s in db.list_servers():
        if s["note"] == DEMO_MARK:
            db.delete_server(s["id"])
            print(f"已删除: {s['name']}")
        elif s["name"].startswith("demo-"):
            db.delete_server(s["id"])
            print(f"已删除: {s['name']}")


if __name__ == "__main__":
    if "--clean" in sys.argv:
        clean()
    else:
        seed()
