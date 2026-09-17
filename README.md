# NPU-Superviser — 昇腾服务器资源看板

开发组内部用的局域网网页:自动采集每台服务器上 `npu-smi info` 的卡状态
(AI Core 利用率 / HBM 显存 / 功耗 / 温度),手动登记卡的占用,查看服务器 SSH 账号密码。

## 功能

- **看板**:每台服务器一张卡片,每张 NPU 一个彩色卡格 — 空闲(青绿)/ 已占用(洋红)/
  在跑未登记(琥珀)/ 离线(灰);顶部汇总统计,支持按型号、组网标签、状态、名称筛选
- **自动采集**:后台定时 SSH 执行 `npu-smi info`(默认 60s,可配),采集失败标记离线并显示原因;
  单台可启停采集,可手动"立即采集"
- **占用登记**:点击卡格登记占用人 / 用途 / 开始时间,一键释放;一张卡同时只有一条生效登记
- **密码查看**:服务器卡片上默认遮挡,点击眼睛显示,一键复制
- **台账备份**:网页导出 / 导入 JSON(含密码,妥善保管)

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

1. 「添加服务器」录入名称 / IP / SSH 端口 / 用户名 / 密码,选型号(默认卡数自动带出,
   实际卡数与显存以采集为准),填组网标签(如 `RoCE, 参数面`)
2. 保存后等首轮采集(约几秒~20s),或点服务器卡片上的 ⟳ 立即采集
3. 占卡:点击卡格 → 填占用人 / 用途 → 「登记占用」;释放:再点开 → 「释放占用」

## 常见问题

- **别人打不开网页**:九成是防火墙,按上面命令放行 8666 端口
- **采集失败显示 Timed out**:服务器 ping 不通或 SSH 端口不通
- **认证失败**:核对用户名密码;服务器要求密钥登录的暂不支持
- **解析失败**:服务器上手动跑 `npu-smi info` 把输出发给维护者,解析器在
  `collector.py`,各 CANN 版本样例与自测在文件底部(`python collector.py` 自测)
- **数据存在哪**:同目录 `data.db`(SQLite)。备份可停服后直接拷文件,或用网页"备份"导出 JSON。
  不要把 data.db 放网络共享盘上(WAL 模式不可靠)
- **安全**:密码明文存库、无登录、全网卡监听 — **只能内网用,严禁映射公网**

## 技术栈

Flask + waitress + paramiko + SQLite;前端原生 HTML/JS/CSS,无构建、无外部 CDN(内网可用)。
