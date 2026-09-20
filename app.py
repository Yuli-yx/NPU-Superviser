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
import ledger_csv
from request_limits import RequestLimiter

STATIC_DIR = Path(__file__).resolve().parent / "static"
PORT = 8666

app = Flask(__name__, static_folder=None)
app.json.ensure_ascii = False
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 + 65536
app.config["RATE_LIMIT_ENABLED"] = True
request_limiter = RequestLimiter()


@app.before_request
def limit_requests():
    if not app.config["RATE_LIMIT_ENABLED"]:
        return None
    peer = request.remote_addr or "unknown"
    policies = []
    if request.path == "/":
        policies = [((peer, "page-burst"), 5, 10), ((peer, "page"), 20, 60)]
    elif request.path.startswith("/api/"):
        policies = [((peer, "api"), 120, 60)]
        if request.path == "/api/dashboard":
            policies += [((peer, "dashboard-burst"), 5, 10), ((peer, "dashboard"), 20, 60)]
        if request.path == "/api/ledger.csv":
            policies.append(((peer, "csv-export"), 10, 60))
        if request.method == "POST" and (request.path == "/api/collect" or request.endpoint == "api_server_collect"):
            policies.append((("global", request.path), 1, 30))
        if request.method == "PUT" and request.path == "/api/config":
            policies.append(((peer, "settings"), 3, 60))
    retry = request_limiter.check(policies) if policies else 0
    if retry:
        return jsonify(error=f"操作过于频繁，请 {retry} 秒后再试", retry_after=retry), 429, {"Retry-After": str(retry)}


@app.before_request
def validate_json_object():
    if request.is_json and not isinstance(request.get_json(silent=True), dict):
        return jsonify(error="请求体必须是 JSON 对象"), 400


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    if isinstance(e, ValueError):
        return jsonify(error=str(e)), 400
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

def _validate_groups(data):
    for key in ("name", "ip", "model", "username", "password", "note"):
        if key in data and not isinstance(data[key], str):
            raise ValueError(f"{key} 必须是文字")
    for key, lo, hi in (("ssh_port", 1, 65535), ("expected_cards", 0, 64)):
        if key in data and data[key] not in (None, ""):
            try:
                data[key] = int(data[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} 必须是整数")
            if not lo <= data[key] <= hi:
                raise ValueError(f"{key} 必须在 {lo}–{hi} 之间")
    if "collect_enabled" in data and (type(data["collect_enabled"]) not in (bool, int) or data["collect_enabled"] not in (0, 1)):
        raise ValueError("启用采集必须是 true 或 false")
    if "tags" in data and (not isinstance(data["tags"], list) or any(not isinstance(t, str) for t in data["tags"])):
        raise ValueError("组网标签必须是文字列表")
    if "network_groups" not in data:
        return data
    groups = data["network_groups"]
    if not isinstance(groups, dict) or set(groups) - {"uboe", "roce", "ubg"}:
        raise ValueError("互通组仅支持 UBoE、RoCE、UBG")
    if any(not isinstance(v, str) or len(v) > 60 for v in groups.values()):
        raise ValueError("互通组名称必须是 60 个字符以内的文字")
    data["network_groups"] = {k: v.strip() for k, v in groups.items() if v.strip()}
    return data

def _server_payload() -> dict:
    data = request.get_json(force=True, silent=True) or {}
    for key in ("name", "ip"):
        if not isinstance(data.get(key), str):
            raise ValueError(f"{key} 必须是文字")
    if not str(data.get("name") or "").strip():
        raise ValueError("名称不能为空")
    if not str(data.get("ip") or "").strip():
        raise ValueError("IP 不能为空")
    if "ssh_port" in data and data["ssh_port"] not in (None, ""):
        data["ssh_port"] = int(data["ssh_port"])
        if not 1 <= data["ssh_port"] <= 65535:
            raise ValueError("SSH 端口必须在 1–65535 之间")
    if "expected_cards" in data and data["expected_cards"] not in (None, ""):
        data["expected_cards"] = int(data["expected_cards"])
        if not 0 <= data["expected_cards"] <= 64:
            raise ValueError("预设卡数必须在 0–64 之间")
    if "tags" in data and not isinstance(data["tags"], list):
        data["tags"] = [t.strip() for t in str(data["tags"]).split(",") if t.strip()]
    return _validate_groups(data)


@app.get("/api/servers")
def api_servers_list():
    return jsonify(db.list_servers())


@app.put("/api/servers/order")
def api_servers_order():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids")
    if not isinstance(ids, list) or any(type(sid) is not int for sid in ids):
        return jsonify(error="排序参数不合法"), 400
    try:
        db.reorder_servers(ids)
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(ok=True)


@app.post("/api/servers")
def api_server_create():
    data = _server_payload()
    if db.get_server_by_name(data["name"].strip()):
        return jsonify(error="服务器名称已存在"), 409
    server = db.create_server(data)
    return jsonify(server), 201


@app.put("/api/servers/<int:sid>")
def api_server_update(sid):
    if not db.get_server(sid):
        return jsonify(error="服务器不存在"), 404
    data = _server_payload()
    existing = db.get_server_by_name(data["name"].strip())
    if existing and existing["id"] != sid:
        return jsonify(error="服务器名称已存在"), 409
    return jsonify(db.update_server(sid, data))


@app.patch("/api/servers/<int:sid>")
def api_server_patch(sid):
    if not db.get_server(sid):
        return jsonify(error="服务器不存在"), 404
    data = request.get_json(force=True, silent=True) or {}
    _validate_groups(data)
    if "collect_enabled" in data:
        data["collect_enabled"] = 1 if data["collect_enabled"] else 0
    return jsonify(db.update_server(sid, _validate_groups(data)))


@app.delete("/api/servers/<int:sid>")
def api_server_delete(sid):
    db.delete_server(sid)
    return jsonify(ok=True)


@app.post("/api/servers/<int:sid>/collect")
def api_server_collect(sid):
    """同步采集单台,响应即结果。"""
    if not db.get_server(sid):
        return jsonify(error="服务器不存在"), 404
    try:
        result = collector.collect_server_now(sid)
    except collector.CollectionInProgress as exc:
        return jsonify(error=str(exc)), 409
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
    if not db.get_server(server_id):
        return jsonify(error="服务器不存在"), 404
    if not db.get_conn().execute("SELECT 1 FROM card WHERE server_id = ? AND npu_id = ?", (server_id, npu_id)).fetchone():
        return jsonify(error="卡不存在，请刷新看板"), 400
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
    if not isinstance(config, dict):
        return jsonify(error="配置格式不合法"), 400
    bounds = {"interval_seconds": (5, 3600), "collect_workers": (1, 32),
              "ssh_connect_timeout": (1, 60), "ssh_exec_timeout": (1, 120),
              "aicore_threshold": (0, 100), "hbm_threshold_pct": (0, 100)}
    for key, (lo, hi) in bounds.items():
        if key in config:
            try:
                number = int(config[key])
            except (TypeError, ValueError):
                return jsonify(error=f"{key} 必须是整数"), 400
            if not lo <= number <= hi:
                return jsonify(error=f"{key} 必须在 {lo}–{hi} 之间"), 400
            config[key] = str(number)
    if "models" in data:
        if not isinstance(data["models"], list):
            return jsonify(error="型号配置必须是列表"), 400
        models = []
        names = set()
        for m in data["models"]:
            if not isinstance(m, dict):
                return jsonify(error="型号配置格式不合法"), 400
            name = str(m.get("name") or "").strip()
            if name:
                key = "".join(name.split()).casefold().translate(str.maketrans("（）", "()"))
                if key in names:
                    return jsonify(error="型号名称重复，请勿通过大小写或空格创建重复分类"), 400
                names.add(key)
                try:
                    cards = int(m.get("cards") or 0)
                except (TypeError, ValueError):
                    return jsonify(error="默认卡数必须是整数"), 400
                if not 0 <= cards <= 64:
                    return jsonify(error="默认卡数必须在 0–64 之间"), 400
                models.append({"name": name, "cards": cards})
        config["models_json"] = json.dumps(models, ensure_ascii=False)
    allowed = set(db.DEFAULT_CONFIG)
    config = {k: v for k, v in config.items() if k in allowed}
    if config:
        db.set_config(config)
    collector_singleton.wake()  # 间隔热生效
    return jsonify(ok=True)


# ---------------------------------------------------------------- 导入/导出

@app.get("/api/ledger.csv")
def api_ledger_export():
    template = request.args.get("template") == "1"
    servers = db.list_servers()
    models = list(dict.fromkeys([m["name"] for m in db.get_models()] + [s["model"] for s in servers if s["model"]]))
    groups = {kind: sorted({s["network_groups"][kind] for s in servers if s["network_groups"].get(kind)})
              for kind in ("uboe", "roce", "ubg")}
    content = ledger_csv.export_csv([] if template else servers, models=models, tags=db.all_tags(), groups=groups)
    filename = "npu-template.csv" if template else "npu-servers.csv"
    return app.response_class(content, mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    })


@app.post("/api/ledger/import")
def api_ledger_import():
    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").lower().endswith(".csv"):
        return jsonify(error="请选择 CSV 文件；Excel 中可另存为 CSV UTF-8"), 400
    try:
        models = list(dict.fromkeys([m["name"] for m in db.get_models()] + [s["model"] for s in db.list_servers() if s["model"]]))
        rows = ledger_csv.parse_csv(upload.read(), models=models)
        result = db.import_server_rows(rows)
    except (ValueError, ledger_csv.csv.Error) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(**result)

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
            "network_groups": raw.get("network_groups") or {},
        }
        _validate_groups(item)
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
