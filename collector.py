"""NPU 采集器:npu-smi 输出解析 + SSH 采集 + 后台定时线程。

解析策略(兼容 CANN 23.x ~ 25.x 已核实的三代输出,详见 selftest 样例):
- npu-smi 的表格每张 NPU 占两行:第 1 行是卡级信息(NPU 编号、芯片名、Health、
  功耗、温度、大页),第 2 行是 chip 级信息(Chip 号、Bus-Id、AICore%、显存 used/total);
- 按 "|" 切列,行分类:第 3 列为纯字母(OK/Warning/...) => 卡行,否则为 chip 行;
- HBM 的 used/total 取 chip 行上最后一个 "x / y" 数对(新格式);
  若 chip 行没有,回退取卡行上的数对(老 910 格式 HBM-Usage 在第一行);
- 多 chip(多 die)聚合:AICore 取 max,HBM used/total 求和;
- 结构化解析失败时退化为纯正则逐行配对,仍失败则报 parse_error(带原始输出)。
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import db

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
PAIR_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
BUSID_RE = re.compile(r"\d{4}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.\d")
VERSION_RE = re.compile(r"npu-smi\s+v?(\d[\w.\-]*)")

# exec_command 走非登录 shell,PATH 里可能没有 npu-smi,用驱动工具目录兜底
SSH_CMD = ("npu-smi info 2>&1 || /usr/local/Ascend/driver/tools/npu-smi info 2>&1")


def _num(text, idx=0):
    found = NUM_RE.findall(text)
    try:
        return float(found[idx])
    except (IndexError, ValueError):
        return None


def _is_alpha(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]+", s or ""))


def _is_busid(s: str) -> bool:
    return bool(BUSID_RE.search(s or ""))


def parse_npu_smi(text: str) -> dict:
    """解析 `npu-smi info` 输出。返回 {version, cards, parse_error, raw}。

    真实输出里 NPU 编号与芯片名常挤在同一单元格("0  910B3"),
    Bus-Id 在卡行后一行;此处同时兼容"同格"与"分列"两种布局。
    """
    text = ANSI_RE.sub("", text or "").replace("\r", "")
    version = None
    m = VERSION_RE.search(text)
    if m:
        version = m.group(1)

    cards = []
    cur = None

    def close(card):
        if card:
            _aggregate(card)
            if card.get("npu_id") is not None:
                cards.append(card)

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line.startswith("|") or line.startswith("+-") or line.startswith("+="):
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]

        # --- 卡行判定:某一列是纯字母的 Health 词 ---
        row = None
        if len(parts) >= 4 and parts[0].isdigit() and _is_alpha(parts[2]):
            # 四列布局:NPU | Name | Health | 数据
            row = (int(parts[0]), parts[1] or None, parts[2], " ".join(parts[3:]))
        elif len(parts) >= 3 and _is_alpha(parts[1]):
            # 三列布局:"NPU Name" | Health | 数据
            toks = parts[0].split()
            if toks and toks[0].isdigit():
                row = (int(toks[0]), " ".join(toks[1:]) or None, parts[1],
                       " ".join(parts[2:]))
        if row:
            close(cur)
            npu_id, chip_name, health, tail = row
            nums = NUM_RE.findall(tail)
            cur = {
                "npu_id": npu_id, "chip_name": chip_name, "health": health,
                "power_w": float(nums[0]) if len(nums) >= 1 else None,
                "temp_c": float(nums[1]) if len(nums) >= 2 else None,
                "_card_pair": None, "_chips": [],
            }
            pm = PAIR_RE.search(tail)
            if pm:
                cur["_card_pair"] = (int(pm.group(1)), int(pm.group(2)))
            continue

        # --- chip 行判定:Bus-Id 列在其后,尾部是 AICore% 与显存 used/total ---
        if cur is not None:
            tail = None
            if len(parts) >= 4 and _is_busid(parts[2]):
                tail = " ".join(parts[3:])
            elif len(parts) >= 3 and (_is_busid(parts[1]) or parts[1] == ""):
                tail = " ".join(parts[2:])
            if tail is not None:
                pairs = PAIR_RE.findall(tail)
                cur["_chips"].append({
                    "aicore_pct": _num(tail),
                    "pair": (int(pairs[-1][0]), int(pairs[-1][1])) if pairs else None,
                })
    close(cur)

    if cards:
        return {"version": version, "cards": cards, "parse_error": False, "raw": None}

    cards = _fallback_parse(text)
    if cards:
        return {"version": version, "cards": cards, "parse_error": False, "raw": None}
    return {"version": version, "cards": [], "parse_error": True,
            "raw": text[:500] if text else "npu-smi 无输出"}


def _aggregate(card: dict) -> None:
    """多 chip 聚合:AICore 取 max,HBM used/total 求和;无 chip 数据回退卡行数对。"""
    chips = card.pop("_chips", [])
    card_pair = card.pop("_card_pair", None)
    aicores = [c["aicore_pct"] for c in chips if c["aicore_pct"] is not None]
    pairs = [c["pair"] for c in chips if c["pair"]]
    if pairs:
        card["hbm_used_mb"] = sum(p[0] for p in pairs)
        card["hbm_total_mb"] = sum(p[1] for p in pairs)
    elif card_pair:
        card["hbm_used_mb"], card["hbm_total_mb"] = card_pair
    else:
        card["hbm_used_mb"] = card["hbm_total_mb"] = None
    card["aicore_pct"] = max(aicores) if aicores else None


def _fallback_parse(text: str) -> list:
    """兜底:纯正则逐行配对(未来格式大改时的最后防线)。"""
    cards = []
    lines = text.split("\n")
    card_row_re = re.compile(
        r"^\|\s*(\d+)\s+(\S+)\s*\|\s*(OK|Warning|Alarm|Critical|Unknown|Caution|ABNORMAL)"
        r"\s*\|\s*(\d+(?:\.\d+)?)\s+(\d+)", re.IGNORECASE)
    chip_row_re = re.compile(r"(\d+)\s+(\d+)\s*/\s*(\d+)")
    for i, line in enumerate(lines):
        m = card_row_re.match(line.strip())
        if not m:
            continue
        card = {"npu_id": int(m.group(1)), "chip_name": m.group(2),
                "health": m.group(3), "power_w": float(m.group(4)),
                "temp_c": float(m.group(5)), "aicore_pct": None,
                "hbm_used_mb": None, "hbm_total_mb": None}
        for nxt in lines[i + 1:i + 3]:
            cm = chip_row_re.findall(nxt)
            if cm:
                last = cm[-1]
                card["aicore_pct"] = _num(nxt.split("|")[-1])
                card["hbm_used_mb"], card["hbm_total_mb"] = int(last[1]), int(last[2])
                break
        cards.append(card)
    return cards


# ---------------------------------------------------------------- SSH 采集

def collect_one(server: dict, connect_timeout: float = 8, exec_timeout: float = 20) -> dict:
    """SSH 到一台服务器执行 npu-smi info。永不抛异常,统一返回 {ok, cards?, error?}。"""
    try:
        import paramiko
    except ImportError as e:  # pragma: no cover - 环境问题
        return {"ok": False, "error": f"paramiko 未安装: {e}"}

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=server["ip"], port=server.get("ssh_port") or 22,
            username=server.get("username") or "root",
            password=server.get("password") or None,
            timeout=connect_timeout, banner_timeout=connect_timeout,
            auth_timeout=connect_timeout,
            allow_agent=False, look_for_keys=False,
        )
        _, stdout, _ = client.exec_command(SSH_CMD, timeout=exec_timeout)
        out = stdout.read().decode("utf-8", "replace")
        rc = stdout.channel.recv_exit_status()
        if not out.strip():
            return {"ok": False, "error": f"npu-smi 退出码 {rc} 且无输出(检查驱动/权限)"}
        parsed = parse_npu_smi(out)
        if parsed["parse_error"] or not parsed["cards"]:
            return {"ok": False, "error": "npu-smi 输出解析失败",
                    "raw": parsed.get("raw")}
        return {"ok": True, "cards": parsed["cards"], "version": parsed["version"]}
    except Exception as e:  # 网络不通/认证失败/超时等
        return {"ok": False, "error": str(e) or e.__class__.__name__}
    finally:
        try:
            client.close()
        except Exception:
            pass


def persist_result(server_id: int, result: dict) -> dict:
    """把一次采集结果写库(成功更新卡快照,失败只记状态)。"""
    db.set_collect_result(server_id, result["ok"], result.get("error"),
                          result.get("version"))
    if result["ok"]:
        db.replace_cards(server_id, result["cards"], int(time.time()))
    return result


def collect_server_now(server_id: int) -> dict:
    """同步采集单台(网页"立即采集"用),结果直接落库。"""
    server = db.get_server(server_id)
    if not server:
        return {"ok": False, "error": "服务器不存在"}
    result = collect_one(
        server,
        connect_timeout=db.get_config_int("ssh_connect_timeout", 8),
        exec_timeout=db.get_config_int("ssh_exec_timeout", 20),
    )
    return persist_result(server_id, result)


# ---------------------------------------------------------------- 定时线程

class Collector:
    """后台 daemon 线程:每 interval 秒并发采集所有启用的服务器,间隔热更新。"""

    def __init__(self):
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._round_lock = threading.Lock()
        self._thread = None
        self._pool = None

    def start(self):
        self._pool = ThreadPoolExecutor(
            max_workers=db.get_config_int("collect_workers", 8),
            thread_name_prefix="npu-collect")
        self._thread = threading.Thread(target=self._loop, name="npu-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def wake(self):
        """唤醒循环,立即开始下一轮(改配置/手动触发后调用)。"""
        self._wake.set()

    def _loop(self):
        # 启动后先等 2 秒再采首轮,让 waitress 先把 HTTP 服务起来
        self._wake.wait(2)
        while not self._stop.is_set():
            try:
                self.run_round()
            except Exception as e:  # 单轮异常不能杀死调度线程
                print(f"[collector] 采集轮异常: {e}")
            interval = db.get_config_int("interval_seconds", 60)
            self._wake.wait(max(5, interval))
            self._wake.clear()

    def run_round(self):
        if not self._round_lock.acquire(blocking=False):
            return  # 上一轮还没跑完,跳过
        try:
            servers = [s for s in db.list_servers() if s["collect_enabled"]]
            if not servers:
                return
            connect_timeout = db.get_config_int("ssh_connect_timeout", 8)
            exec_timeout = db.get_config_int("ssh_exec_timeout", 20)
            futures = {self._pool.submit(collect_one, s, connect_timeout, exec_timeout): s
                       for s in servers}
            for fut, server in futures.items():
                result = fut.result()
                try:
                    persist_result(server["id"], result)
                except Exception as e:
                    print(f"[collector] 写库失败 {server['name']}: {e}")
        finally:
            self._round_lock.release()


# ---------------------------------------------------------------- selftest

SAMPLE_CANN23 = """+-----------------------------------------------------------------------------------------------------------+
| npu-smi 23.1.0                            Version: 23.1.0                                                 |
+===========================+===============+===============================================================+
| NPU     Name              | Health        | Power(W)     Temp(C)           Hugepages-Usage(page)           |
| Chip    Device            | Bus-Id        | AICore(%)    Memory-Usage(MB)                                  |
+===========================+===============+===============================================================+
| 0       910B3             | OK            | 97.0         42                0    / 0                        |
| 0       0                 | 0000:C1:00.0  | 0            0    / 32768                                      |
+===========================+===============+===============================================================+
| 1       910B3             | OK            | 96.8         41                0    / 0                        |
| 0       0                 | 0000:C2:00.0  | 87           30240  / 32768                                    |
+===========================+===============+===============================================================+
"""

SAMPLE_CANN24 = """+-----------------------------------------------------------------------------------------------------------+
| npu-smi 24.1.0                            Version: 24.1.0.3                                               |
+===========================+===============+===============================================================+
| NPU     Name              | Health        | Power(W)     Temp(C)           Hugepages-Usage(page)           |
| Chip    Device  OS-Disk   | Bus-Id        | AICore(%)    HBM-Usage(MB)                                     |
+===========================+===============+===============================================================+
| 0       910B4             | OK            | 92.5         41                0    / 0                       |
| 0       0                 | 0000:81:00.0  | 0            11832 / 65536                                     |
+===========================+===============+===============================================================+
| 1       910B4             | Warning       | 188.2        63                0    / 0                       |
| 0       0                 | 0000:82:00.0  | 96           40960 / 65536                                     |
+===========================+===============+===============================================================+
"""

SAMPLE_A3_16C = """+-----------------------------------------------------------------------------------------------------------+
| npu-smi 25.1.0                            Version: 25.1.RC1                                               |
+===========================+===============+===============================================================+
| NPU     Name              | Health        | Power(W)     Temp(C)           Hugepages-Usage(page)           |
| Chip    Device            | Bus-Id        | AICore(%)    HBM-Usage(MB)                                      |
+===========================+===============+===============================================================+
| 0       910_93C0          | OK            | 95.3         42                0    / 0                       |
| 0       NA                | 0000:C1:00.0  | 12           20480 / 131072                                     |
+===========================+===============+===============================================================+
| 1       910_93C0          | OK            | 95.1         42                0    / 0                       |
| 0       NA                | 0000:C2:00.0  | 0            0 / 131072                                         |
+===========================+===============+===============================================================+
| 15      910_93C0          | OK            | 94.8         41                0    / 0                       |
| 0       NA                | 0000:FF:00.0  | 3            1024 / 131072                                      |
+===========================+===============+===============================================================+
"""


def _selftest():
    r23 = parse_npu_smi(SAMPLE_CANN23)
    assert r23["version"] == "23.1.0" and len(r23["cards"]) == 2, r23
    c0, c1 = r23["cards"]
    assert c0["npu_id"] == 0 and c0["chip_name"] == "910B3" and c0["health"] == "OK"
    assert c0["power_w"] == 97.0 and c0["temp_c"] == 42.0
    assert c0["aicore_pct"] == 0 and c0["hbm_used_mb"] == 0 and c0["hbm_total_mb"] == 32768
    assert c1["aicore_pct"] == 87 and c1["hbm_used_mb"] == 30240

    r24 = parse_npu_smi(SAMPLE_CANN24)
    assert r24["version"] == "24.1.0" and len(r24["cards"]) == 2, r24
    c0, c1 = r24["cards"]
    assert c0["chip_name"] == "910B4" and c0["hbm_used_mb"] == 11832 and c0["hbm_total_mb"] == 65536
    assert c1["health"] == "Warning" and c1["aicore_pct"] == 96 and c1["temp_c"] == 63

    ra3 = parse_npu_smi(SAMPLE_A3_16C)
    assert len(ra3["cards"]) == 3, ra3
    assert ra3["cards"][0]["chip_name"] == "910_93C0"
    assert ra3["cards"][0]["hbm_total_mb"] == 131072 and ra3["cards"][0]["aicore_pct"] == 12
    assert ra3["cards"][2]["npu_id"] == 15 and ra3["cards"][2]["hbm_used_mb"] == 1024

    bad = parse_npu_smi("some garbage output\nwithout table")
    assert bad["parse_error"] and bad["cards"] == []

    # 老款 910(CANN 20.x)格式:HBM 在卡行,AICore 在 chip 行,走兜底路径
    old = parse_npu_smi(
        "| NPU   Name       | Health | Power(W)  Temp(C)   HBM-Usage(MB)   |\n"
        "| Chip             | Bus-Id | AICore(%)  HBM(Mem)  HBM Util(%)     |\n"
        "| 0    910         | OK     | 67.0     40          1024 / 32768    |\n"
        "| 0                | 0000:82:00.0 | 0        0         0%          |\n")
    assert len(old["cards"]) == 1, old
    c = old["cards"][0]
    assert c["npu_id"] == 0 and c["health"] == "OK"
    assert c["hbm_used_mb"] == 1024 and c["hbm_total_mb"] == 32768

    print("parse selftest: 全部通过")
    for sample, name in ((SAMPLE_CANN23, "CANN23"), (SAMPLE_CANN24, "CANN24"),
                         (SAMPLE_A3_16C, "A3-16卡")):
        cards = parse_npu_smi(sample)["cards"]
        print(f"  {name}: {len(cards)} 张卡,"
              f" 例: {cards[0]['chip_name']} AICore={cards[0]['aicore_pct']}%")


if __name__ == "__main__":
    _selftest()
