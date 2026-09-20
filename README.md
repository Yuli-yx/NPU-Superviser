# NPU-Superviser — 昇腾服务器资源看板

开发组内部用的局域网网页:自动采集每台服务器上 `npu-smi info` 的卡状态
(AI Core 利用率 / HBM 显存 / 功耗 / 温度),手动登记卡的占用,查看服务器 SSH 账号密码。

<img width="986" height="788" alt="image" src="https://github.com/user-attachments/assets/4f0a7bc8-e7e0-409d-ac90-91ea7da900b1" />


## 功能

- **看板**:每台服务器一张卡片,每张 NPU 一个彩色卡格 — 空闲(青绿)/ 已占用(洋红)/
  在跑未登记(琥珀)/ 离线(灰);A5/A3 分区，支持按型号、组网标签、互通组、状态、名称筛选。
  拖动卡片左上角 ⠿ 可调整同型号分区内的机器顺序，顺序写库、所有人共享；聚焦手柄后也可用方向键调整。
- **自动采集**:后台定时 SSH 执行 `npu-smi info`(默认 30 分钟,可配),采集失败标记离线并显示原因;
  单台可启停采集,可手动"立即采集"
- **占用登记**:点击卡格登记占用人 / 用途 / 开始时间,一键释放;一张卡同时只有一条生效登记
- **密码查看**:服务器卡片上默认遮挡,点击眼睛显示,一键复制
- **台账导入导出**:网页下载 CSV 模板、导入 / 导出 CSV（可用 Excel 编辑；含密码，妥善保管）。
- **网络互通组**:UBoE、RoCE、UBG 各自独立标记；同网络、同组名表示人工确认互通，留空表示未知。
  网络类型相同不代表互通。此标记不执行实时网络检测。

## 部署(Windows)

```bat
cd C:\Users\willzhu\Projects\NPU-Superviser
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py
```

或直接双击 `start.bat`(首次自动建虚拟环境并装依赖)。

启动后监听 `0.0.0.0:8666`,本机访问 <http://localhost:8666>。
局域网同事访问 `http://<本机IP>:8666`(用 `ipconfig` 查本机 IP)。

### 放行防火墙(管理员,一次性)

```bat
netsh advfirewall firewall add rule name="NPU Supervisor" dir=in action=allow protocol=TCP localport=8666
```

### 开机自启(可选)

```bat
schtasks /create /tn NPUSupervisor /tr "C:\Users\willzhu\Projects\NPU-Superviser\.venv\Scripts\python.exe C:\Users\willzhu\Projects\NPU-Superviser\app.py" /sc onstart /ru SYSTEM
```

## 演示数据

为了先看到看板效果,库里预置了 3 台 `demo-` 开头的演示服务器(已停采,静态假数据)。
正式使用前清掉:

```bat
.venv\Scripts\python scripts\demo_seed.py --clean
```

## 使用说明

卡格以可独立使用的设备为单位：带 `Phy-ID` 的 `npu-smi` 输出按物理设备编号展示。
A3 每个 NPU 的两个 die 分别显示，共 16 个卡格（编号 0–15），各自显示显存、利用率与占用登记。

1. 「添加服务器」录入名称 / IP / SSH 端口 / 用户名 / 密码,选型号(默认卡数自动带出,
   实际卡数与显存以采集为准),填组网标签(如 `RoCE, 参数面`)
2. 保存后点服务器卡片上的 ⟳ 立即采集，或等待后台定时采集。页面每 30 分钟自动读取看板数据，
   「刷新看板」仅读库，「立即采集」才执行 SSH；右上角不再显示倒计时。
3. 占卡:点击卡格 → 填占用人 / 用途 → 「登记占用」;释放:再点开 → 「释放占用」

### 批量台账

「台账导入 / 导出」→「下载空模板」，用 Excel 打开填写，另存为 **CSV UTF-8** 后上传。
仅 IP 必填，名称为空时使用 IP；型号填写 A3 或 A5 对应型号以便分区。
模板列名动态列出当前型号选项、已有互通组、是/否及默认值。型号导入会统一大小写、空格及全角括号，
未知型号会拒绝导入，请先在页面设置中添加。旧版简短列名仍可导入。
SSH 端口默认 22，用户名默认 root，默认启用采集；密码、网络互通组等按需填写。
按名称匹配已有机器。可选字段为空时保留原值，清空已有字段请在编辑页面操作。
整份文件校验通过才原子导入，最多 1000 台 / 2 MB；错误会提示行号。
导出 CSV 不包含占用登记、卡快照或型号配置，不能替代完整数据库备份。
旧 `/api/export`、`/api/import` JSON 接口保留用于兼容既有备份。

### 防重复与限流

手动刷新看板间隔至少 5 秒，同一次写操作请求未完成时禁止重复提交。
查看密码仅更新当前密码区域，不重新绘制看板；无变化的机器卡片不移动 DOM。
服务端按真实连接 IP 限流，不信任客户端的 `X-Forwarded-For`：
看板最多 5 次/10 秒、20 次/分钟；整页刷新最多 5 次/10 秒、20 次/分钟；
CSV 下载最多 10 次/分钟，所有 API 合计最多 120 次/分钟。
同一台机器手动采集、全量采集各自最多触发 1 次/30 秒（跨用户共享），
且同台机器的手动和定时采集互斥。被限流返回 HTTP 429 和 `Retry-After`，不继续执行采集。
当前限流为单进程内存实现；多进程部署需共享限流存储。反向代理部署需明确配置可信代理，否则按代理 IP 计数。

### 回归测试

`.venv\Scripts\python -m unittest discover -s scripts -p 'test_*.py'`
使用内存库和 Mock，不修改实际台账，不访问真实 SSH。
浏览器隔离测试服务：`.venv\Scripts\python scripts\web_test_server.py`，
访问 `http://127.0.0.1:8667`，仅有临时假数据、假采集，不启动真实 SSH。
旧 `scripts/api_test.py` 会修改它指向的服务配置，不要直接对生产 8666 运行。

## 常见问题

- **别人打不开网页**:九成是防火墙,按上面命令放行 8666 端口
- **采集失败显示 Timed out**:服务器 ping 不通或 SSH 端口不通
- **认证失败**:核对用户名密码;服务器要求密钥登录的暂不支持
- **解析失败**:服务器上手动跑 `npu-smi info` 把输出发给维护者,解析器在
  `collector.py`,各 CANN 版本样例与自测在文件底部(`python collector.py` 自测)
- **数据存在哪**:同目录 `data.db`(SQLite)。完整备份使用 SQLite backup API，或停服后复制数据库及残留 WAL；
  网页 CSV 仅是服务器台账。数据库备份及运行日志放在 `logs/`，其中备份含 SSH 密码，需妥善保管。
  不要把 data.db 放网络共享盘上(WAL 模式不可靠)
- **安全**:密码明文存库、无登录、全网卡监听 — **只能内网用,严禁映射公网**

## 技术栈

Flask + waitress + paramiko + SQLite;前端原生 HTML/JS/CSS,无构建、无外部 CDN(内网可用)。
