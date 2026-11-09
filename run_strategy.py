#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket BTC 5分钟涨跌量化策略引擎与 Web 决策监控中心
入口文件：run_strategy.py

用法：
  python3 run_strategy.py             # 启动策略引擎与 Web 面板 (默认端口 8888)
  python3 run_strategy.py --port 8080 # 指定 Web 面板端口
"""

import argparse
import os
import signal
import sys
import time

from btc_strategy.auth_store import AuthStore
from btc_strategy.binance_feed import BinanceFeed
from btc_strategy.candle_engine import CandleEngine
from btc_strategy.chainlink_poller import ChainlinkPoller
from btc_strategy.config import WEB_HOST, WEB_PORT
from btc_strategy.polymarket_service import PolymarketService
from btc_strategy.round_manager import RoundManager
from btc_strategy.system_monitor import SystemMonitor
from btc_strategy.traffic_stats import TrafficStats
from btc_strategy.web_server import WebDashboardServer

GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RED = "\033[91m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"


class AppContext:

  def __init__(self, candles, feed, chainlink, rounds, monitor, traffic):
    self.candles = candles
    self.feed = feed
    self.chainlink = chainlink
    self.rounds = rounds
    self.monitor = monitor
    self.traffic = traffic

  def get_full_status(self) -> dict:
    feed_stats = self.feed.get_stats()
    feed_stats["binance_price"] = round(self.candles.last_price, 2)
    sys_stats = self.monitor.get_stats()
    cl_stats = self.chainlink.get_state()

    connections = [
        {
            "id": "binance_ws",
            "name": "Binance 现货行情专线",
            "type": "WebSocket (wss)",
            "endpoint": feed_stats.get("endpoint", "stream.binance.com:9443"),
            "status": "HEALTHY" if feed_stats.get("connected") else "DISCONNECTED",
            "status_text": "已连接 (稳定)" if feed_stats.get("connected") else "断开重连中",
            "rtt_ms": feed_stats.get("ping_rtt_ms", 0.0),
            "detail": f"{feed_stats.get('msg_rate', 0)} msg/s ({feed_stats.get('byte_rate_kb', 0)} KB/s)",
            "sub_detail": f"重连: {feed_stats.get('reconnect_count', 0)} 次 | 累计: {feed_stats.get('msg_total', 0)} 帧"
        },
        {
            "id": "chainlink_rpc",
            "name": "Chainlink 结算预言机",
            "type": "JSON-RPC (eth_call)",
            "endpoint": cl_stats.get("rpc_host") or "1rpc.io/matic",
            "status": "HEALTHY" if cl_stats.get("price", 0) > 0 else "UNREADY",
            "status_text": "数据正常" if cl_stats.get("price", 0) > 0 else "等待同步",
            "rtt_ms": cl_stats.get("rpc_rtt_ms", 0.0),
            "detail": f"链上延时: {cl_stats.get('age_sec', 0)}s",
            "sub_detail": f"读取成功: {cl_stats.get('fetch_count', 0)} 次 | 错误: {cl_stats.get('error_count', 0)}"
        },
        {
            "id": "polymarket_clob",
            "name": "Polymarket 撮合引擎",
            "type": "TLS / CLOB Gateway",
            "endpoint": "ws-subscriptions-clob.polymarket.com",
            "status": sys_stats.get("clob_status", "CONNECTING"),
            "status_text": "链路极佳" if sys_stats.get("clob_status") == "HEALTHY" else ("延迟偏高" if sys_stats.get("clob_status") == "HIGH_LATENCY" else "探测中"),
            "rtt_ms": sys_stats.get("clob_rtt_ms", 0.0),
            "detail": "443 极速通道",
            "sub_detail": "CLOB 订单簿订阅端点"
        },
        {
            "id": "clock_sync",
            "name": "交易所原子钟校准",
            "type": "HTTP NTP Sync",
            "endpoint": "data-api.binance.vision",
            "status": sys_stats.get("clock_status", "SYNCED"),
            "status_text": "毫秒级同步" if sys_stats.get("clock_status") == "SYNCED" else "时钟漂移警告",
            "rtt_ms": abs(sys_stats.get("clock_offset_ms", 0.0)),
            "detail": f"偏移: {sys_stats.get('clock_offset_ms', 0):+.2f} ms",
            "sub_detail": "防时间漂移拒单"
        }
    ]

    return {
        "current_round": self.rounds.get_current(),
        "upcoming_round": self.rounds.get_upcoming(),
        "stats": self.rounds.get_stats(),
        "history": self._history_with_current(),
        "system": sys_stats,
        "feed_stats": feed_stats,
        "chainlink": cl_stats,
        "connections": connections,
        "traffic": self.traffic.snapshot(feed_stats.get("bytes_total", 0)),
        "server_time": time.time(),
    }

  def _history_with_current(self) -> list:
    """历史列表顶部带上尚未结算的当前场次。"""
    history = self.rounds.get_history(limit=50)
    current = self.rounds.get_current()
    open_ts = current.get("open_ts")
    if not open_ts or any(row.get("open_ts") == open_ts for row in history):
      return history
    live = dict(current)
    live["live"] = True
    return [live] + history


def get_public_ip():
  try:
    import urllib.request
    req = urllib.request.Request("https://api.ipify.org", headers={"User-Agent": "curl/7.68.0"})
    with urllib.request.urlopen(req, timeout=2) as resp:
      return resp.read().decode().strip()
  except Exception:
    return "你的服务器公网IP"

def print_banner(port: int):
  pub_ip = get_public_ip()
  print(f"\n{BOLD}{CYAN}{'='*80}{RESET}")
  print(f"{BOLD}  ⚡ POLYMARKET BTC 5M 涨跌量化决策引擎 & 实时 WEB 监控中心{RESET}")
  print(f"{BOLD}{CYAN}{'='*80}{RESET}")
  print(
      f"  • {BOLD}Binance 实时行情流{RESET} : wss://stream.binance.com:9443 (aggTrade"
      " 现货)"
  )
  print(
      f"  • {BOLD}Chainlink 结算预言机{RESET}: 智能多源 Polygon RPC 节点轮询"
      " (eth_call)"
  )
  print(
      f"  • {BOLD}信号决策算法模型{RESET} : 开场前锁定 (1h趋势 + 已收盘5m +"
      " 开场前斜率)"
  )
  print(
      f"  • {BOLD}Web 决策监控面板{RESET}   : {GREEN}{BOLD}http://{pub_ip}:{port}{RESET}"
      f" (浏览器直接打开)"
  )
  print(f"{BOLD}{CYAN}{'='*80}{RESET}\n")
  print(f"{YELLOW}正在连接行情数据流与链上节点，请稍候...{RESET}\n")


def main():
  parser = argparse.ArgumentParser(description="Polymarket BTC 5m 量化策略")
  parser.add_argument(
      "--port", type=int, default=WEB_PORT, help="Web 监控端口 (默认 8888)"
  )
  parser.add_argument(
      "--host", type=str, default=WEB_HOST, help="Web 监听地址 (默认 0.0.0.0)"
  )
  args = parser.parse_args()

  start_ts = time.time()

  # 1. 初始化各子模块
  candles = CandleEngine()
  feed = BinanceFeed(on_tick_cb=candles.on_tick)
  chainlink = ChainlinkPoller()
  rounds = RoundManager(candle_engine=candles, chainlink_poller=chainlink)
  monitor = SystemMonitor(start_time=start_ts)

  traffic = TrafficStats()
  auth = AuthStore()
  polymarket = PolymarketService()
  app_ctx = AppContext(candles, feed, chainlink, rounds, monitor, traffic)
  web_server = WebDashboardServer(args.host, args.port, app_ctx, auth, traffic, polymarket)

  print_banner(args.port)

  # 2. 依次启动后台服务
  feed.start()
  chainlink.start()
  rounds.start()
  web_server.start()

  # 3. 优雅退出控制
  def sig_handler(sig, frame):
    print(f"\n{YELLOW}收到停止信号，正在保存数据并关闭服务...{RESET}")
    feed.stop()
    chainlink.stop()
    rounds.stop()
    web_server.stop()
    sys.exit(0)

  signal.signal(signal.SIGINT, sig_handler)
  signal.signal(signal.SIGTERM, sig_handler)

  # 4. 终端控制台摘要输出循环
  last_print_ts = 0
  last_round_id = 0

  while True:
    time.sleep(2)
    now = time.time()
    if now - last_print_ts >= 10:
      cur = rounds.get_current()
      up = rounds.get_upcoming()
      st = rounds.get_stats()
      bp = candles.last_price
      cp = chainlink.price
      pred = cur.get("predicted_dir") or "NO_CALL"
      conf = cur.get("confidence") or 0.0
      rem = cur.get("time_remaining") or 0
      up_pred = up.get("predicted_dir") or "WAITING"
      up_in = up.get("time_until_open") or 0

      pred_color = (
          GREEN
          if pred == "UP"
          else (RED if pred == "DOWN" else (YELLOW if pred == "SKIP" else RESET))
      )
      rem_str = f"{rem//60:02d}:{rem%60:02d}"

      print(
          f"[{time.strftime('%H:%M:%S')}] "
          f"BTC: ${bp:,.2f} | "
          f"Chainlink: ${cp:,.2f} | "
          f"当前场次[{cur.get('open_time_str','--')} 剩{rem_str}] "
          f"本场: {pred_color}{BOLD}{pred}{RESET} ({conf:.1f}%) | "
          f"下一场: {BOLD}{up_pred}{RESET} ({up_in}s) | "
          f"胜率: {GREEN}{st['accuracy_pct']}%{RESET} ({st['correct']}/{st['called_rounds']})"
      )
      last_print_ts = now


if __name__ == "__main__":
  main()
