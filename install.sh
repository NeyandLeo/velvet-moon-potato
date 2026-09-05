#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v conda >/dev/null 2>&1; then
    echo "未找到 conda，请先安装 Miniconda 或 Anaconda。"
    exit 1
fi

echo "创建或更新 conda 环境 maoyan_grabber..."
conda env update -n maoyan_grabber -f environment.yml --prune

echo "安装完成。工具优先使用系统 Google Chrome。"
echo "如系统没有 Chrome，请再运行："
echo "  conda run -n maoyan_grabber playwright install chromium"
echo "启动命令：./start.sh"
