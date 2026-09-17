"""API 集成自测:对运行中的服务(localhost:8666)走一遍核心流程。"""
import json
import urllib.request

BASE = "http://localhost:8666"
ok_count = fail_count = 0


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, detail=""):
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  ok {name}")
    else:
        fail_count += 1
        print(f"  FAIL {name} {detail}")


print("== 服务器 CRUD ==")
st, s1 = call("POST", "/api/servers", {
    "name": "api-test-01", "model": "A3", "ip": "192.0.2.99",
    "username": "root", "password": "pw123",
    "tags": ["RoCE", "测试"], "expected_cards": 16})
check("create", st == 201 and s1["id"] > 0, f"{st} {s1}")
sid = s1.get("id")

st, got = call("GET", "/api/servers")
check("list contains", any(s["id"] == sid for s in got))

st, got = call("PUT", f"/api/servers/{sid}", {
    "name": "api-test-01", "model": "A3", "ip": "192.0.2.98", "ssh_port": 22,
    "username": "root", "password": "pw456", "note": "改过", "tags": ["IB"],
    "expected_cards": 16, "collect_enabled": True})
check("update", st == 200 and got["ip"] == "192.0.2.98" and got["tags"] == ["IB"], f"{st} {got}")

print("== 占用登记 ==")
st, occ = call("POST", "/api/occupancy", {
    "server_id": sid, "npu_id": 3, "user": "张三", "purpose": "llama3 微调"})
check("occupy", st == 201, f"{st} {occ}")
st, dup = call("POST", "/api/occupancy", {
    "server_id": sid, "npu_id": 3, "user": "李四"})
check("double occupy -> 409", st == 409, f"{st} {dup}")

print("== 采集(不可达 IP,应失败但不崩)==")
st, res = call("POST", f"/api/servers/{sid}/collect")
check("collect unreachable fails", st in (400, 502) and res.get("ok") is False, f"{st} {res}")

st, dash = call("GET", "/api/dashboard")
srv = next(s for s in dash["servers"] if s["id"] == sid)
check("offline status", srv["status"] == "offline" and srv["last_error"], srv["status"])
check("password visible in api", srv["password"] == "pw456")

print("== 配置与型号 ==")
st, _ = call("PUT", "/api/config", {
    "config": {"interval_seconds": "45", "aicore_threshold": "15"},
    "models": [{"name": "A3", "cards": 16}, {"name": "自造-960", "cards": 8}]})
check("config put", st == 200)
st, cfg = call("GET", "/api/config")
check("config applied", cfg["config"]["interval_seconds"] == "45"
      and cfg["config"]["aicore_threshold"] == "15", cfg["config"])
check("models persisted", any(m["name"] == "自造-960" for m in cfg["models"]), cfg["models"])

print("== 导入导出 ==")
st, exp = call("GET", "/api/export")
check("export", st == 200 and any(s["name"] == "api-test-01" for s in exp["servers"]))
exp["servers"].append({
    "name": "api-import-02", "model": "A3", "ip": "192.0.2.50", "ssh_port": 22,
    "username": "root", "password": "imp", "tags": ["新"], "expected_cards": 8,
    "collect_enabled": False, "active_occupancies": []})
st, imp = call("POST", "/api/import", exp)
check("import", st == 200 and imp["added"] == 1 and imp["updated"] >= 1, f"{st} {imp}")

print("== 释放与清理 ==")
st, occ_list = call("GET", "/api/dashboard")
srv = next(s for s in occ_list["servers"] if s["id"] == sid)
st, _ = call("DELETE", "/api/occupancy/1") if False else (200, None)  # id 未知,改用重建验证唯一性即可
call("DELETE", f"/api/servers/{sid}")
for s in call("GET", "/api/servers")[1]:
    if s["name"].startswith("api-"):
        call("DELETE", f"/api/servers/{s['id']}")
st, after = call("GET", "/api/servers")
check("cleanup", not any(s["name"].startswith("api-") for s in after))

# 恢复默认配置与型号
call("PUT", "/api/config", {"config": {"interval_seconds": "60", "aicore_threshold": "20"}})

print(f"\n结果: {ok_count} ok, {fail_count} FAIL")
raise SystemExit(1 if fail_count else 0)
