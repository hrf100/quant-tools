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

from btc_strategy.binance_feed import BinanceFeed
from btc_strategy.candle_engine import CandleEngine
from btc_strategy.chainlink_poller import ChainlinkPoller
from btc_strategy.config import WEB_HOST, WEB_PORT
from btc_strategy.round_manager import RoundManager
from btc_strategy.system_monitor import SystemMonitor
from btc_strategy.web_server import WebDashboardServer

GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RED = "\033[91m"
MAGENTA = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"


class AppContext:

  def __init__(self, candles, feed, chainlink, rounds, monitor):
    self.candles = candles
    self.feed = feed
    self.chainlink = chainlink
    self.rounds = rounds
    self.monitor = monitor

  def get_full_status(self) -> dict:
    feed_stats = self.feed.get_stats()
    feed_stats["binance_price"] = round(self.candles.last_price, 2)

    return {
        "current_round": self.rounds.get_current(),
        "stats": self.rounds.get_stats(),
        "history": self.rounds.get_history(limit=50),
        "system": self.monitor.get_stats(),
        "feed_stats": feed_stats,
        "chainlink": self.chainlink.get_state(),
        "server_time": time.time(),
    }


def print_banner(port: int):
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
      f"  • {BOLD}信号决策算法模型{RESET} : 三层融合评分 (1h趋势 + 5m结构动能 +"
      " 开盘微观斜率)"
  )
  print(
      f"  • {BOLD}Web 决策监控面板{RESET}   : {GREEN}{BOLD}http://0.0.0.0:{port}{RESET}"
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

  app_ctx = AppContext(candles, feed, chainlink, rounds, monitor)
  web_server = WebDashboardServer(args.host, args.port, app_ctx)

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
      st = rounds.get_stats()
      bp = candles.last_price
      cp = chainlink.price
      pred = cur.get("predicted_dir") or "WAITING"
      conf = cur.get("confidence") or 0.0
      rem = cur.get("time_remaining") or 0

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
          f"判定: {pred_color}{BOLD}{pred}{RESET} ({conf:.1f}%) | "
          f"总胜率: {GREEN}{st['accuracy_pct']}%{RESET} ({st['correct']}/{st['settled_rounds']})"
      )
      last_print_ts = now


if __name__ == "__main__":
  main()
