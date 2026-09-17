"""SQLite 数据访问层:建库、配置、服务器台账、NPU 卡快照、占用登记。

约定:时间一律存 epoch 秒(UTC),由前端按浏览器本地时区展示。
连接策略:WAL + 每线程独立连接(threading.local)+ busy_timeout,
Flask 请求线程与采集线程并发读写短事务,当前规模(几十台服务器)足够。
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data.db"

_local = threading.local()

DEFAULT_MODELS = [
    {"name": "A3", "cards": 16},
    {"name": "A5-850(950PR)", "cards": 8},
    {"name": "A5-850E(950DT)", "cards": 8},
    {"name": "A5-950SuperPod(950DT)", "cards": 8},
    {"name": "A5-PC16(950DT)", "cards": 16},
]

DEFAULT_CONFIG = {
    "interval_seconds": "60",       # 定时采集间隔(秒)
    "aicore_threshold": "20",       # AI Core 利用率超过该值视为"在跑"
    "hbm_threshold_pct": "10",      # HBM 已用百分比超过该值视为"在跑"
    "ssh_connect_timeout": "8",
    "ssh_exec_timeout": "20",
    "collect_workers": "8",         # 并发采集线程数
    "models_json": json.dumps(DEFAULT_MODELS, ensure_ascii=False),
}

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS config (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS server (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL,
  model           TEXT NOT NULL DEFAULT '',
  ip              TEXT NOT NULL,
  ssh_port        INTEGER NOT NULL DEFAULT 22,
  username        TEXT NOT NULL DEFAULT 'root',
  password        TEXT NOT NULL DEFAULT '',
  note            TEXT NOT NULL DEFAULT '',
  tags            TEXT NOT NULL DEFAULT '',        -- 逗号分隔,如 "roce,双平面"
  expected_cards  INTEGER,                         -- 台账预设卡数,展示以采集为准
  collect_enabled INTEGER NOT NULL DEFAULT 1,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending', -- pending / online / offline
  last_error      TEXT,
  last_collect_ts INTEGER,
  npu_smi_version TEXT
);

CREATE TABLE IF NOT EXISTS card (
  server_id    INTEGER NOT NULL REFERENCES server(id) ON DELETE CASCADE,
  npu_id       INTEGER NOT NULL,
  chip_name    TEXT,
  health       TEXT,
  aicore_pct   REAL,
  hbm_used_mb  INTEGER,
  hbm_total_mb INTEGER,
  power_w      REAL,
  temp_c       REAL,
  updated_at   INTEGER NOT NULL,
  PRIMARY KEY (server_id, npu_id)
);

CREATE TABLE IF NOT EXISTS occupancy (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  server_id  INTEGER NOT NULL REFERENCES server(id) ON DELETE CASCADE,
  npu_id     INTEGER NOT NULL,
  user       TEXT NOT NULL,
  purpose    TEXT NOT NULL DEFAULT '',
  start_ts   INTEGER NOT NULL,
  end_ts     INTEGER,                               -- NULL = 占用中
  created_at INTEGER NOT NULL
);
-- 一张卡同时只有一条生效登记,数据库层硬保证
CREATE UNIQUE INDEX IF NOT EXISTS idx_occupancy_active
  ON occupancy(server_id, npu_id) WHERE end_ts IS NULL;
"""


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = get_conn()
    with conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_CONFIG.items():
            conn.execute(
                "INSERT OR IGNORE INTO config(key, value) VALUES(?, ?)", (key, value)
            )


# ---------------------------------------------------------------- 配置

def get_all_config() -> dict:
    rows = get_conn().execute("SELECT key, value FROM config").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_config(key: str, default: str = "") -> str:
    row = get_conn().execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_config_int(key: str, default: int) -> int:
    try:
        return int(get_config(key, str(default)))
    except (TypeError, ValueError):
        return default


def set_config(mapping: dict) -> None:
    conn = get_conn()
    with conn:
        conn.executemany(
            "INSERT INTO config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, str(v)) for k, v in mapping.items()],
        )


def get_models() -> list:
    try:
        models = json.loads(get_config("models_json", "[]"))
        return models if isinstance(models, list) else DEFAULT_MODELS
    except ValueError:
        return DEFAULT_MODELS


# ---------------------------------------------------------------- 服务器台账

SERVER_FIELDS = ("name", "model", "ip", "ssh_port", "username", "password",
                 "note", "tags", "expected_cards", "collect_enabled")


def _split_tags(raw: str) -> list:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _server_to_dict(row) -> dict:
    d = dict(row)
    d["tags"] = _split_tags(d.get("tags", ""))
    d["collect_enabled"] = bool(d.get("collect_enabled"))
    return d


def list_servers() -> list:
    rows = get_conn().execute("SELECT * FROM server ORDER BY name").fetchall()
    return [_server_to_dict(r) for r in rows]


def get_server(server_id: int) -> dict | None:
    row = get_conn().execute("SELECT * FROM server WHERE id = ?", (server_id,)).fetchone()
    return _server_to_dict(row) if row else None


def get_server_by_name(name: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM server WHERE name = ?", (name,)).fetchone()
    return _server_to_dict(row) if row else None


def create_server(data: dict) -> dict:
    now = int(time.time())
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "INSERT INTO server(name, model, ip, ssh_port, username, password, note, tags,"
            " expected_cards, collect_enabled, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (data.get("name", "").strip(), data.get("model", ""), data.get("ip", "").strip(),
             int(data.get("ssh_port") or 22), data.get("username") or "root",
             data.get("password") or "", data.get("note") or "",
             ",".join(data.get("tags") or []), data.get("expected_cards"),
             1 if data.get("collect_enabled", True) else 0, now, now),
        )
        server_id = cur.lastrowid
    return get_server(server_id)


def update_server(server_id: int, data: dict) -> dict | None:
    fields = {k: data[k] for k in SERVER_FIELDS if k in data}
    if not fields:
        return get_server(server_id)
    if "tags" in fields:
        fields["tags"] = ",".join(fields["tags"] or [])
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [int(time.time()), server_id]
    conn = get_conn()
    with conn:
        conn.execute(f"UPDATE server SET {sets}, updated_at = ? WHERE id = ?", values)
    return get_server(server_id)


def delete_server(server_id: int) -> None:
    conn = get_conn()
    with conn:
        conn.execute("DELETE FROM server WHERE id = ?", (server_id,))


def all_tags() -> list:
    rows = get_conn().execute("SELECT tags FROM server WHERE tags != ''").fetchall()
    seen = []
    for r in rows:
        for t in _split_tags(r["tags"]):
            if t not in seen:
                seen.append(t)
    return sorted(seen)


# ---------------------------------------------------------------- 卡快照与采集结果

def replace_cards(server_id: int, cards: list, ts: int) -> None:
    """一轮采集成功后,整体替换该服务器的卡快照(不保留历史)。"""
    conn = get_conn()
    with conn:
        conn.execute("DELETE FROM card WHERE server_id = ?", (server_id,))
        conn.executemany(
            "INSERT INTO card(server_id, npu_id, chip_name, health, aicore_pct,"
            " hbm_used_mb, hbm_total_mb, power_w, temp_c, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(server_id, c["npu_id"], c.get("chip_name"), c.get("health"),
              c.get("aicore_pct"), c.get("hbm_used_mb"), c.get("hbm_total_mb"),
              c.get("power_w"), c.get("temp_c"), ts) for c in cards],
        )


def set_collect_result(server_id: int, ok: bool, error: str | None,
                       version: str | None = None) -> None:
    status = "online" if ok else "offline"
    conn = get_conn()
    with conn:
        conn.execute(
            "UPDATE server SET status = ?, last_error = ?, last_collect_ts = ?,"
            " npu_smi_version = COALESCE(?, npu_smi_version) WHERE id = ?",
            (status, None if ok else (error or "未知错误")[:500],
             int(time.time()), version, server_id),
        )


# ---------------------------------------------------------------- 占用登记

class OccupiedError(Exception):
    """该卡已有生效登记。"""


def occupy(server_id: int, npu_id: int, user: str, purpose: str,
           start_ts: int | None = None) -> int:
    now = int(time.time())
    conn = get_conn()
    with conn:
        try:
            cur = conn.execute(
                "INSERT INTO occupancy(server_id, npu_id, user, purpose, start_ts, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (server_id, npu_id, user.strip(), (purpose or "").strip(),
                 start_ts or now, now),
            )
        except sqlite3.IntegrityError as e:
            raise OccupiedError("该卡已有生效登记,请先释放") from e
        return cur.lastrowid


def release(occupancy_id: int) -> bool:
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "UPDATE occupancy SET end_ts = ? WHERE id = ? AND end_ts IS NULL",
            (int(time.time()), occupancy_id),
        )
        return cur.rowcount > 0


def active_occupancies() -> dict:
    """{(server_id, npu_id): occupancy_row} — 全部生效中的登记。"""
    rows = get_conn().execute(
        "SELECT * FROM occupancy WHERE end_ts IS NULL ORDER BY start_ts"
    ).fetchall()
    return {(r["server_id"], r["npu_id"]): dict(r) for r in rows}


def active_occupancies_of(server_id: int) -> dict:
    return {npu: occ for (sid, npu), occ in active_occupancies().items() if sid == server_id}


def release_by_card(server_id: int, npu_id: int) -> bool:
    conn = get_conn()
    with conn:
        cur = conn.execute(
            "UPDATE occupancy SET end_ts = ? WHERE server_id = ? AND npu_id = ?"
            " AND end_ts IS NULL",
            (int(time.time()), server_id, npu_id),
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------- 看板聚合

def build_dashboard() -> dict:
    cfg = get_all_config()
    try:
        aicore_thr = float(cfg.get("aicore_threshold", 20))
    except ValueError:
        aicore_thr = 20.0
    try:
        hbm_thr_pct = float(cfg.get("hbm_threshold_pct", 10))
    except ValueError:
        hbm_thr_pct = 10.0
    try:
        interval = int(cfg.get("interval_seconds", 60))
    except ValueError:
        interval = 60

    servers = list_servers()
    actives = active_occupancies()
    conn = get_conn()

    summary = {"servers_total": len(servers), "servers_online": 0, "cards_total": 0,
               "idle": 0, "occupied": 0, "busy": 0, "offline": 0, "unhealthy": 0}

    for s in servers:
        if s["status"] == "online":
            summary["servers_online"] += 1
        rows = conn.execute(
            "SELECT * FROM card WHERE server_id = ? ORDER BY npu_id", (s["id"],)
        ).fetchall()
        cards = []
        for r in rows:
            c = dict(r)
            occ = actives.get((s["id"], c["npu_id"]))
            hbm_total = c.get("hbm_total_mb") or 0
            hbm_pct = (c.get("hbm_used_mb") or 0) * 100.0 / hbm_total if hbm_total else 0.0
            aicore = c.get("aicore_pct")
            if s["status"] != "online":
                st = "offline"
            elif occ:
                st = "occupied"
            elif (aicore is not None and aicore >= aicore_thr) or hbm_pct >= hbm_thr_pct:
                st = "busy"
            else:
                st = "idle"
            if c.get("health") and c["health"] not in ("OK", "NA", None, ""):
                summary["unhealthy"] += 1
            summary[st if st in ("idle", "occupied", "busy", "offline") else "idle"] += 1
            summary["cards_total"] += 1
            cards.append({
                "npu_id": c["npu_id"], "chip_name": c.get("chip_name"),
                "health": c.get("health"), "aicore_pct": aicore,
                "hbm_used_mb": c.get("hbm_used_mb"), "hbm_total_mb": c.get("hbm_total_mb"),
                "power_w": c.get("power_w"), "temp_c": c.get("temp_c"),
                "updated_at": c.get("updated_at"),
                "occupancy": ({"id": occ["id"], "user": occ["user"],
                               "purpose": occ["purpose"], "start_ts": occ["start_ts"]}
                              if occ else None),
                "state": st,
            })
        s["cards"] = cards
        s.pop("tags_raw", None)

    return {
        "summary": summary,
        "thresholds": {"aicore": aicore_thr, "hbm_pct": hbm_thr_pct},
        "interval_seconds": interval,
        "servers": servers,
    }
