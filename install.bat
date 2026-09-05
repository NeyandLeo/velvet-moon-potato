@echo off
setlocal
cd /d "%~dp0"

where conda >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo 未找到 conda，请先安装 Miniconda 或 Anaconda。
    exit /b 1
)

echo 创建或更新 conda 环境 maoyan_grabber...
call conda env update -n maoyan_grabber -f environment.yml --prune
if %ERRORLEVEL% NEQ 0 exit /b 1

echo 安装完成。工具优先使用系统 Google Chrome。
echo 如系统没有 Chrome，请运行：conda run -n maoyan_grabber playwright install chromium
echo 启动命令：start.bat
endlocal
