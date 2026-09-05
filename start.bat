@echo off
REM 快速启动脚本
cd /d "%~dp0"

REM 激活 conda 环境
call conda activate maoyan_grabber

if %ERRORLEVEL% NEQ 0 (
    echo ❌ 环境激活失败，请先运行 install.bat
    pause
    exit /b 1
)

REM 启动服务
echo 启动猫眼电影票监控助手...
echo 访问地址: http://127.0.0.1:5000
echo 按 Ctrl+C 停止服务
echo.

python app.py
pause
