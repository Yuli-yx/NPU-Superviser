@echo off
rem NPU 资源看板一键启动:首次运行自动创建虚拟环境并安装依赖
cd /d %~dp0
if not exist .venv (
  echo [首次运行] 创建虚拟环境并安装依赖...
  python -m venv .venv
  .venv\Scripts\pip install -r requirements.txt
)
echo 启动 NPU 资源看板: http://localhost:8666
.venv\Scripts\python app.py
pause
