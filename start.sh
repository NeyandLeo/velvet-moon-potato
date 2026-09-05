#!/bin/bash
# 快速启动脚本
cd "$(dirname "$0")" || exit 1

# 激活 conda 环境
source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate maoyan_grabber

if [ $? -ne 0 ]; then
    echo "❌ 环境激活失败，请先运行 ./install.sh"
    exit 1
fi

# 启动服务
echo "🚀 启动猫眼抢票工具..."
echo "访问地址: http://localhost:5000"
echo "按 Ctrl+C 停止服务"
echo ""

exec python app.py
