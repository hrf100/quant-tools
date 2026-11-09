#!/usr/bin/env bash
# 没有 pip 时会先装 python3-venv，再在 .venv 里安装依赖并启动。
set -euo pipefail

PORT=8888
if [ -n "${1:-}" ]; then
  PORT=$1
fi

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "需要 Python 3.10 或更高版本。当前是：$(python3 --version 2>&1)"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  if ! python3 -m venv .venv; then
    sudo apt-get update
    sudo apt-get install -y python3-venv
    rm -rf .venv
    python3 -m venv .venv
  fi
fi

.venv/bin/python -m pip install -r requirements.txt

PUB_IP=$(curl -fsS --max-time 3 https://api.ipify.org || hostname -I | awk '{print $1}')
echo "================================================================="
echo "  ⚡ 正在启动 Polymarket BTC 5m 量化决策引擎..."
echo "  🌐 Web 控制面板: http://${PUB_IP}:${PORT}"
echo "================================================================="

exec .venv/bin/python run_strategy.py --port "$PORT"
