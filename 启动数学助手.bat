@echo off
chcp 65001 >nul
title 数学学习 AI 助手（新版）
cd /d "%~dp0"

if not exist ".env" (
    echo [提示] 未找到 .env 文件，请复制 .env.example 为 .env 并填写 DEEPSEEK_API_KEY。
    pause
    exit /b 1
)

if not exist "frontend\dist\index.html" (
    echo [提示] 未找到前端构建产物 frontend\dist\index.html。
    echo 请先在 VS Code 终端进入 frontend 目录，执行已有的构建命令：
    echo     cd frontend
    echo     npm run build
    echo 构建完成后再运行本脚本。
    pause
    exit /b 1
)

echo 正在启动数学学习 AI 助手（新版），浏览器会自动打开，请稍候...
echo 手机访问地址请看启动完成后控制台打印的"局域网地址"。

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" api.py --host 0.0.0.0 --port 8000
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [错误] 未找到可用的 Python：项目 .venv 不存在，系统 PATH 中也没有 python。
        pause
        exit /b 1
    )
    python api.py --host 0.0.0.0 --port 8000
)
set "LAUNCH_EXIT=%errorlevel%"

if not "%LAUNCH_EXIT%"=="0" (
    echo [提示] 启动失败，退出码 %LAUNCH_EXIT%。请查看上方错误信息（如端口被占用、依赖未安装）。
)
pause
exit /b %LAUNCH_EXIT%
