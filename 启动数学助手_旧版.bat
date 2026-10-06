@echo off
chcp 65001 >nul
title 数学学习 AI 助手
cd /d "%~dp0"

if not exist ".env" (
    echo [提示] 未找到 .env 文件，请复制 .env.example 为 .env 并填写 DEEPSEEK_API_KEY。
    pause
    exit /b 1
)

for /f "tokens=2 delims=:" %%i in ('ipconfig ^| findstr IPv4') do set "LAN_IP=%%i"
if defined LAN_IP set "LAN_IP=%LAN_IP: =%"
if not defined LAN_IP set "LAN_IP=无法自动获取，请用 ipconfig 查看"

netstat -ano | findstr ":7860" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo ⚠️ 检测到助手已在运行。若想加载最新版本，请先关闭旧窗口再重新启动；否则直接打开现有页面。
    echo 助手已经在运行，直接打开页面...
    start "" "http://127.0.0.1:7860"
    echo 手机访问：http://%LAN_IP%:7860（手机需连接同一 Wi-Fi）
    exit /b 0
)

echo 正在启动数学学习 AI 助手，浏览器会自动打开，请稍候...
echo 手机访问：http://%LAN_IP%:7860（手机需连接同一 Wi-Fi）
echo If the page is not updated, close the old browser tab and reopen http://127.0.0.1:7860
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" app.py
) else (
    python app.py
)
pause
