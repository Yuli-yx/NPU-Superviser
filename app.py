"""NPU 资源看板 — Flask 入口。

启动:python app.py  →  waitress 监听 0.0.0.0:8666,内网直接访问。
注意:绝不开 debug=True(reloader 会双进程,采集线程起两份)。
"""
import json
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

import collector
import db

STATIC_DIR = Path(__file__).resolve().parent / "static"
PORT = 8666

app = Flask(__name__, static_folder=None)
app.json.ensure_ascii = False


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    return jsonify(error=str(e)), 500


# ---------------------------------------------------------------- 静态页

@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC_DIR, name)


# ---------------------------------------------------------------- 看板数据

@app.get("/api/dashboard")
def api_dashboard():
    return jsonify(db.build_dashboard())


# ---------------------------------------------------------------- 服务器台账

def _server_payload() -> dict:
    data = request.get_json(force=True, silent=True) or {}
    if not str(data.get("name") or "").strip():
        raise ValueError("名称不能为空")
    if not str(data.get("ip") or "").strip():
        raise ValueError("IP 不能为空")
    if "ssh_port" in data and data["ssh_port"] not in (None, ""):
        data["ssh_port"] = int(data["ssh_port"])
    if "tags" in data and not isinstance(data["tags"], list):
        data["tags"] = [t.strip() for t in str(data["tags"]).split(",") if t.strip()]
    return data


@app.get("/api/servers")
def api_servers_list():
    return jsonify(db.list_servers())


@app.post("/api/servers")
def api_server_create():
    server = db.create_server(_server_payload())
    return jsonify(server), 201


@app.put("/api/servers/<int:sid>")
def api_server_update(sid):
    if not db.get_server(sid):
        return jsonify(error="服务器不存在"), 404
    return jsonify(db.update_server(sid, _server_payload()))


@app.patch("/api/servers/<int:sid>")
def api_server_patch(sid):
    if not db.get_server(sid):
        return jsonify(error="服务器不存在"), 404
    data = request.get_json(force=True, silent=True) or {}
    if "collect_enabled" in data:
        data["collect_enabled"] = 1 if data["collect_enabled"] else 0
    return jsonify(db.update_server(sid, data))


@app.delete("/api/servers/<int:sid>")
def api_server_delete(sid):
    db.delete_server(sid)
    return jsonify(ok=True)


@app.post("/api/servers/<int:sid>/collect")
def api_server_collect(sid):
    """同步采集单台,响应即结果。"""
    if not db.get_server(sid):
        return jsonify(error="服务器不存在"), 404
    result = collector.collect_server_now(sid)
    if result["ok"]:
        return jsonify(ok=True, cards=len(result["cards"]),
                       version=result.get("version"))
    body = jsonify(ok=False, error=result.get("error"))
    body.status_code = 502 if "Timed out" in str(result.get("error")) else 400
    return body


@app.post("/api/collect")
def api_collect_all():
    """唤醒后台线程,立即开始一轮全量采集(异步)。"""
    collector_singleton.wake()
    return jsonify(started=True)


# ---------------------------------------------------------------- 占用登记

@app.post("/api/occupancy")
def api_occupy():
    data = request.get_json(force=True, silent=True) or {}
    try:
        server_id = int(data.get("server_id"))
        npu_id = int(data.get("npu_id"))
        user = str(data.get("user") or "").strip()
    except (TypeError, ValueError):
        return jsonify(error="参数不合法"), 400
    if not user:
        return jsonify(error="占用人不能为空"), 400
    start_ts = data.get("start_ts")
    try:
        start_ts = int(start_ts) if start_ts else None
    except (TypeError, ValueError):
        return jsonify(error="开始时间不合法"), 400
    try:
        occ_id = db.occupy(server_id, npu_id, user,
                           str(data.get("purpose") or ""), start_ts)
    except db.OccupiedError as e:
        return jsonify(error=str(e)), 409
    return jsonify(id=occ_id), 201


@app.delete("/api/occupancy/<int:occ_id>")
def api_release(occ_id):
    if not db.release(occ_id):
        return jsonify(error="登记不存在或已释放"), 404
    return jsonify(ok=True)


# ---------------------------------------------------------------- 配置

@app.get("/api/config")
def api_config_get():
    cfg = db.get_all_config()
    cfg.pop("models_json", None)
    return jsonify(config=cfg, models=db.get_models(), tags=db.all_tags())


@app.put("/api/config")
def api_config_put():
    data = request.get_json(force=True, silent=True) or {}
    config = data.get("config") or {}
    if "models" in data:
        models = []
        for m in data["models"]:
            name = str(m.get("name") or "").strip()
            if name:
                try:
                    cards = int(m.get("cards") or 0)
                except (TypeError, ValueError):
                    cards = 0
                models.append({"name": name, "cards": cards})
        config["models_json"] = json.dumps(models, ensure_ascii=False)
    allowed = set(db.DEFAULT_CONFIG)
    config = {k: v for k, v in config.items() if k in allowed}
    if config:
        db.set_config(config)
    collector_singleton.wake()  # 间隔热生效
    return jsonify(ok=True)


# ---------------------------------------------------------------- 导入/导出

@app.get("/api/export")
def api_export():
    servers = []
    for s in db.list_servers():
        s = dict(s)
        s["active_occupancies"] = [
            {"npu_id": npu, "user": occ["user"], "purpose": occ["purpose"],
             "start_ts": occ["start_ts"]}
            for (sid, npu), occ in db.active_occupancies().items() if sid == s["id"]
        ]
        servers.append(s)
    return jsonify(app="NPU-Superviser", exported_at=int(time.time()),
                   models=db.get_models(), servers=servers)


@app.post("/api/import")
def api_import():
    data = request.get_json(force=True, silent=True) or {}
    payload_servers = data.get("servers")
    if not isinstance(payload_servers, list):
        return jsonify(error="导入文件格式不对,缺少 servers 列表"), 400
    if isinstance(data.get("models"), list) and data["models"]:
        db.set_config({"models_json": json.dumps(data["models"], ensure_ascii=False)})

    existing = {s["name"]: s["id"] for s in db.list_servers()}
    added = updated = occs = 0
    for raw in payload_servers:
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            continue
        item = {
            "name": raw.get("name"), "model": raw.get("model") or "",
            "ip": raw.get("ip") or "", "ssh_port": raw.get("ssh_port") or 22,
            "username": raw.get("username") or "root",
            "password": raw.get("password") or "", "note": raw.get("note") or "",
            "tags": raw.get("tags") or [], "expected_cards": raw.get("expected_cards"),
            "collect_enabled": bool(raw.get("collect_enabled", True)),
        }
        name = item["name"]
        if name in existing:
            db.update_server(existing[name], item)
            updated += 1
        else:
            created = db.create_server(item)
            existing[name] = created["id"]
            added += 1
        for occ in raw.get("active_occupancies") or []:
            try:
                db.occupy(existing[name], int(occ["npu_id"]),
                          str(occ.get("user") or "").strip(),
                          str(occ.get("purpose") or ""), int(occ.get("start_ts") or 0) or None)
                occs += 1
            except (db.OccupiedError, ValueError, KeyError):
                pass  # 已有生效登记的卡跳过
    return jsonify(added=added, updated=updated, occupancies=occs)


# ---------------------------------------------------------------- 启动

collector_singleton = collector.Collector()


def main():
    db.init_db()
    collector_singleton.start()
    print(f"* NPU 资源看板: http://0.0.0.0:{PORT}  (局域网访问 http://<本机IP>:{PORT})")
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=16)


if __name__ == "__main__":
    main()
