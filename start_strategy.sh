#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Polymarket BTC 5m 涨跌量化策略 & 监控面板 一键启动脚本
# -----------------------------------------------------------------------------

PORT=8888
if [ -n "$1" ]; then
  PORT=$1
fi

# 获取公网 IP (用于提示访问链接)
PUB_IP=$(curl -s https://api.ipify.org || hostname -I | awk '{print $1}')

echo "================================================================="
echo "  ⚡ 正在启动 Polymarket BTC 5m 量化决策引擎..."
echo "  🌐 Web 控制面板: http://${PUB_IP}:${PORT}"
echo "================================================================="

# 确保在脚本所在目录
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# 启动策略
python3 run_strategy.py --port "$PORT"
