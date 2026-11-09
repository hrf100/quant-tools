# btc_strategy/web_server.py
"""
基于 Python 标准库 http.server 的现代量化 Web 仪表盘。
零外部依赖，直接在浏览器中可视化监控：
- 当前局实时判定与打分分解
- 历史场次列表与命中率统计
- 服务器系统资源 (CPU/内存/网络) 与行情数据流健康度 (Binance WS / Chainlink RPC)
"""

import http.server
import json
import socketserver
import threading
from urllib.parse import urlparse

HTML_DASHBOARD = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Polymarket BTC 5m 量化决策中心</title>
  <style>
    :root {
      --bg: #0b0f19;
      --card-bg: #111827;
      --card-border: #1f2937;
      --accent: #3b82f6;
      --up: #10b981;
      --down: #ef4444;
      --skip: #f59e0b;
      --text: #f3f4f6;
      --text-muted: #9ca3af;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", monospace, sans-serif; }
    body { background-color: var(--bg); color: var(--text); padding: 16px; min-height: 100vh; }
    .header { display: flex; padding-bottom: 12px; border-bottom: 1px solid var(--card-border); margin-bottom: 16px; }
    .header-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
    .header-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
    .header h1 { font-size: 1.15rem; font-weight: 700; color: #60a5fa; display: flex; align-items: center; gap: 8px; }
    .header-actions { display: flex; align-items: center; gap: 8px; font-size: 0.85rem; color: var(--text-muted); }
    .metrics { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; font-size: 0.72rem; color: var(--text-muted); }
    .metric { display: inline-flex; align-items: center; gap: 6px; }
    .metric b, .metric span { color: var(--text); }
    .bar-wrap.thin { width: 52px; height: 4px; margin: 0; }
    .bar-wrap.thin .bar-fill { display: block; }
    .ghost-btn { border: 1px solid #374151; background: #111827; color: #e5e7eb; border-radius: 8px; padding: 6px 10px; cursor: pointer; font-size: 0.8rem; }
    .ghost-btn:focus-visible { outline: 2px solid #60a5fa; outline-offset: 2px; }
    .ghost-btn:disabled { opacity: 0.5; cursor: wait; }
    .ghost-btn.danger { color: #fecaca; border-color: #7f1d1d; }
    .history-bar { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-bottom: 12px; }
    .history-name { display: flex; flex-direction: column; gap: 2px; min-width: 72px; }
    .history-name b { font-size: 0.95rem; font-weight: 700; color: var(--text); letter-spacing: 0; text-transform: none; }
    .history-name span { font-size: 0.72rem; color: var(--text-muted); }
    .history-stats { display: flex; align-items: baseline; gap: 14px; flex: 1; flex-wrap: wrap; font-size: 0.78rem; color: var(--text-muted); }
    .history-stats b { color: var(--text); font-weight: 700; }
    .hit-rate { font-size: 1.35rem; font-weight: 800; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color: var(--up); letter-spacing: -0.03em; }
    #clear-history { margin-left: auto; }
    #history-msg { font-size: 0.75rem; color: var(--text-muted); min-height: 1em; }
    .modal { position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 30; display: flex; align-items: center; justify-content: center; padding: 16px; }
    .modal[hidden] { display: none !important; }
    .modal-card { width: min(440px, 100%); background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 16px; }
    .modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
    .decision-grid { grid-template-columns: minmax(0, 1.6fr) minmax(260px, 0.8fr); }
    .decision-trade { display: grid; grid-template-columns: minmax(0, 1fr) 280px; gap: 16px; align-items: start; }
    .trade-col { border-left: 1px solid var(--card-border); padding-left: 14px; }
    @media (max-width: 980px) {
      .decision-grid, .decision-trade { grid-template-columns: 1fr; }
      .trade-col { border-left: 0; padding-left: 0; border-top: 1px solid var(--card-border); padding-top: 12px; }
    }
    .badge { padding: 4px 10px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; }
    .badge-live { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
    
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 20px; }
    .card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; padding: 16px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.3); }
    .card-title { font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }
    
    /* 核心价格看板 */
    .price-val { font-size: 1.8rem; font-weight: 800; font-family: monospace; }
    .sub-info { font-size: 0.8rem; color: var(--text-muted); margin-top: 6px; }

    /* 当前局大卡片 */
    .current-card { border-left: 4px solid var(--accent); }
    .decision-badge { display: inline-flex; align-items: center; justify-content: center; font-size: 1.7rem; font-weight: 900; padding: 10px 24px; border-radius: 10px; margin: 10px 0; letter-spacing: 2px; }
    .dir-UP { background: rgba(16, 185, 129, 0.2); color: var(--up); border: 2px solid var(--up); }
    .dir-DOWN { background: rgba(239, 68, 68, 0.2); color: var(--down); border: 2px solid var(--down); }
    .dir-SKIP { background: rgba(245, 158, 11, 0.2); color: var(--skip); border: 2px solid var(--skip); }
    .dir-WAIT { background: rgba(156, 163, 175, 0.15); color: var(--text-muted); border: 2px dashed #4b5563; font-size: 1.2rem; }

    /* 进度条与指标条 */
    .bar-wrap { background: #1f2937; height: 8px; border-radius: 4px; overflow: hidden; margin-top: 6px; }
    .bar-fill { height: 100%; border-radius: 4px; transition: width 0.3s; }
    
    /* 表格 */
    .table-container { overflow-x: auto; max-height: 480px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; text-align: left; }
    th { padding: 10px; background: #1f2937; color: var(--text-muted); position: sticky; top: 0; font-weight: 600; }
    td { padding: 10px; border-bottom: 1px solid #1f2937; font-family: monospace; }
    tr:hover { background: rgba(255,255,255,0.02); }
    .res-tag { display: inline-block; padding: 2px 6px; border-radius: 4px; font-weight: 700; font-size: 0.75rem; }
    .tag-win { background: rgba(16, 185, 129, 0.2); color: var(--up); }
    .tag-loss { background: rgba(239, 68, 68, 0.2); color: var(--down); }
    .tag-skip { background: rgba(245, 158, 11, 0.2); color: var(--skip); }
    .tag-pend { color: var(--text-muted); }

    .score-table { width: 100%; font-size: 0.8rem; margin-top: 8px; }
    .score-table td { padding: 4px 0; border: none; font-family: sans-serif; }
    .score-val { font-family: monospace; font-weight: 600; text-align: right; }

    /* 核心连接状态矩阵样式 */
    .conn-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin-bottom: 20px; }
    .conn-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 10px; padding: 12px 14px; position: relative; transition: border-color 0.2s; }
    .conn-card:hover { border-color: rgba(96, 165, 250, 0.4); }
    .conn-top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
    .conn-name { font-size: 0.82rem; font-weight: 700; color: #f3f4f6; display: flex; align-items: center; gap: 8px; }
    .conn-type { font-size: 0.65rem; padding: 1px 6px; border-radius: 4px; background: rgba(255,255,255,0.06); color: #9ca3af; font-family: monospace; }
    .conn-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
    .dot-green { background: #10b981; box-shadow: 0 0 8px #10b981; animation: pulse-green 2s infinite; }
    .dot-yellow { background: #f59e0b; box-shadow: 0 0 8px #f59e0b; }
    .dot-red { background: #ef4444; box-shadow: 0 0 8px #ef4444; }
    @keyframes pulse-green { 0% { opacity: 0.4; } 50% { opacity: 1; } 100% { opacity: 0.4; } }
    .conn-rtt { font-size: 1.35rem; font-weight: 800; font-family: monospace; }
    .conn-info { font-size: 0.72rem; color: var(--text-muted); margin-top: 4px; display: flex; justify-content: space-between; }
    .conn-endpoint { font-size: 0.7rem; font-family: monospace; color: #60a5fa; margin-top: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    #auth-gate { position: fixed; inset: 0; background: #0b0f19; display: none; align-items: center; justify-content: center; z-index: 20; }
    #app-root { display: none; }
    .auth-card { width: min(420px, calc(100% - 32px)); background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 24px; }
    .auth-card h2 { font-size: 1.2rem; margin-bottom: 8px; }
    .auth-card input, .acct-form input { width: 100%; margin-top: 10px; padding: 10px 12px; border-radius: 8px; border: 1px solid #374151; background: #0b0f19; color: #f3f4f6; }
    .auth-card button, .acct-form button, .text-btn { margin-top: 14px; border: 0; border-radius: 8px; padding: 10px 14px; background: #2563eb; color: white; font-weight: 700; cursor: pointer; }
    .text-btn { background: transparent; color: #93c5fd; padding: 0; margin: 0 0 0 12px; }
    .trade-card { width: 100%; }
    .trade-bal { display: flex; justify-content: space-between; gap: 8px; font-size: 0.72rem; color: var(--text-muted); margin: -6px 0 8px; }
    .trade-bal b { color: var(--text); font-family: monospace; }
    .trade-market { font-size: 0.68rem; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 8px; }
    .quick-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
    .quick-buy { width: 100%; border: 0; border-radius: 8px; padding: 8px; text-align: left; color: #fff; cursor: pointer; }
    .quick-buy.up { background: #1b7a49; }
    .quick-buy.down { background: #e15b64; }
    .quick-buy:hover:not(:disabled) { filter: brightness(1.06); }
    .quick-buy:disabled { cursor: default; }
    .quick-top { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
    .quick-name { font-size: 0.95rem; font-weight: 700; }
    .quick-px { font-size: 1.15rem; font-weight: 800; }
    .quick-sub { margin-top: 2px; font-size: 0.65rem; line-height: 1.25; opacity: 0.92; }
    .amt-row { display: flex; align-items: center; gap: 6px; margin-top: 8px; }
    .amt-row input { flex: 1; min-width: 0; text-align: center; padding: 6px 8px; border-radius: 8px; border: 1px solid #374151; background: #0b0f19; color: #f3f4f6; }
    .amt-unit { color: var(--text-muted); font-size: 0.8rem; }
    .step-btn, .amt-chips button { border: 1px solid #374151; background: #0b0f19; color: #e5e7eb; border-radius: 8px; cursor: pointer; }
    .step-btn { width: 28px; height: 28px; }
    .amt-chips { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin-top: 8px; }
    .amt-chips button { padding: 4px 0; font-size: 0.72rem; }
    #book-msg { min-height: 1.1em; margin-top: 6px; font-size: 0.72rem; color: #fbbf24; }
    .mini-bars { display: flex; align-items: flex-end; gap: 3px; height: 22px; width: 120px; margin: 0; }
    .mini-bars span { flex: 1; background: #2563eb; min-height: 2px; }
    .secret { font-family: monospace; font-size: 0.75rem; word-break: break-all; color: #d1d5db; }
  </style>
</head>
<body>
  <div id="auth-gate">
    <form class="auth-card" id="auth-form">
      <h2 id="auth-title">登录</h2>
      <p id="auth-hint" style="color:#9ca3af; font-size:0.85rem;">输入账号后进入决策看板。</p>
      <input id="auth-user" autocomplete="username" placeholder="用户名" />
      <input id="auth-pass" type="password" autocomplete="current-password" placeholder="密码" />
      <button type="submit" id="auth-submit">进入</button>
      <div id="auth-error" style="color:#fca5a5; margin-top:10px; font-size:0.85rem;"></div>
    </form>
  </div>

  <div id="app-root">
  <div class="header">
    <div class="header-main">
      <div class="header-row">
        <h1>
          <span>⚡ Polymarket BTC 5m 量化决策中心</span>
          <span class="badge badge-live" id="live-indicator">LIVE</span>
        </h1>
        <div class="header-actions">
          <span id="auth-user-label"></span>
          <button type="button" class="ghost-btn" id="settings-btn">设置</button>
          <button type="button" class="text-btn" id="logout-btn">退出</button>
        </div>
      </div>
      <div class="metrics">
        <span class="metric">CPU <b id="sys-cpu">0%</b><span class="bar-wrap thin"><span id="bar-cpu" class="bar-fill" style="width:0%; background:#10b981;"></span></span></span>
        <span class="metric">内存 <b id="sys-mem">0%</b><span class="bar-wrap thin"><span id="bar-mem" class="bar-fill" style="width:0%; background:#3b82f6;"></span></span></span>
        <span class="metric">运行 <b id="sys-uptime">00:00:00</b></span>
        <span class="metric">Binance <span id="ws-status">--</span></span>
        <span class="metric">Chainlink <span id="cl-status">--</span></span>
        <span class="metric">请求 <b id="tr-req">0</b> · 入 <b id="tr-in">0</b> KB · 出 <b id="tr-out">0</b> KB</span>
        <span class="metric">近 1 分钟 <b id="tr-rpm">0</b> 次 · <b id="tr-out-rate">0</b> KB/s</span>
        <span class="metric">行情 <b id="tr-bn">0</b> MB · <b id="tr-bn-rate">0</b> KB/s</span>
        <span class="mini-bars" id="tr-bars"></span>
        <span class="metric"><span id="sys-os">--</span> · 网卡 <span id="net-in">0 KB/s</span> · RPC <span id="cl-rpc-node">--</span></span>
      </div>
    </div>
  </div>
  <div id="settings-modal" class="modal" hidden>
    <div class="modal-card" role="dialog" aria-label="钱包设置">
      <div class="modal-head">
        <b>钱包设置</b>
        <button type="button" class="ghost-btn" id="settings-close">关闭</button>
      </div>
      <form class="acct-form" id="wallet-form">
        <input id="wallet-key" type="password" placeholder="签名私钥 0x..." autocomplete="off" />
        <input id="wallet-funder" placeholder="中转钱包地址，留空则自动识别" autocomplete="off" />
        <button type="submit">用私钥登录 Polymarket</button>
      </form>
      <div id="acct-view" style="display:none; margin-top:12px; font-size:0.82rem; line-height:1.55;"></div>
      <div id="acct-msg" style="margin-top:8px; font-size:0.8rem; color:#fbbf24;"></div>
    </div>
  </div>

  <!-- 第一排：实时行情与核心胜率 -->
  <div class="grid">
    <div class="card">
      <div class="card-title">Binance 现货实时价 (BTC/USDT)</div>
      <div class="price-val" id="binance-price">$ --,---.--</div>
      <div class="sub-info">消息流速率: <b id="ws-rate">0.0</b> msg/s (<b id="ws-kb">0.0</b> KB/s)</div>
    </div>
    
    <div class="card">
      <div class="card-title">Chainlink 预言机链上价 (BTC/USD)</div>
      <div class="price-val" id="chainlink-price" style="color: #60a5fa;">$ --,---.--</div>
      <div class="sub-info">
        链上更新距今: <b id="cl-age">0.0s</b> | RPC 延迟: <b id="cl-rtt">0ms</b>
      </div>
    </div>

    <div class="card">
      <div class="card-title">预言机结算价差 (Spread)</div>
      <div class="price-val" id="spread-val">--.-- USD</div>
      <div class="sub-info" id="spread-pct">偏离 0.00% (触发阈值 0.50%)</div>
    </div>
  </div>

  <!-- 核心连接链路实时健康矩阵 -->
  <div style="margin-bottom: 10px; font-size: 0.82rem; font-weight: 700; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.05em; display: flex; align-items: center; justify-content: space-between;">
    <span>⚡ 核心量化连接链路实时健康矩阵 (Connection Health Matrix)</span>
    <span style="font-size: 0.72rem; color: #6b7280; text-transform: none;">自动毫秒级心跳检测</span>
  </div>
  <div class="conn-grid" id="conn-matrix">
    <div style="color: var(--text-muted); font-size: 0.8rem; padding: 10px;">正在检测各节点连接链路...</div>
  </div>

  <!-- 第二排：本场判定与快买 -->
  <div class="grid decision-grid">
    <div class="card current-card decision-trade">
    <div class="decision-col">
      <div class="card-title">
        <span>本场开场前预测</span>
        <span id="round-phase" style="font-size:0.75rem; color:#60a5fa; font-weight:700;">--</span>
      </div>
      
      <div style="text-align: center; margin: 12px 0;">
        <div id="dir-badge" class="decision-badge dir-WAIT">等待开场前预测...</div>
        <div id="dir-lock-note" style="font-size: 0.75rem; color: var(--text-muted); margin-bottom: 6px;">开场时刻锁定，本局内不再改</div>
        <div style="font-size: 0.85rem; color: var(--text-muted);">
          置信度: <b id="dir-confidence" style="color:var(--text); font-size:1.1rem;">--%</b> |
          综合打分: <b id="dir-score" style="color:var(--text); font-family:monospace;">0.0</b> / ±100
        </div>
      </div>

      <div style="margin-top: 16px; font-size: 0.85rem;">
        <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
          <span style="color:var(--text-muted);">本局剩余时间</span>
          <b id="round-timer" style="font-family:monospace; color:#38bdf8;">--:--</b>
        </div>
        <div class="bar-wrap">
          <div id="round-timer-bar" class="bar-fill" style="width: 100%; background: #38bdf8;"></div>
        </div>
      </div>

      <div style="margin-top: 14px; font-size: 0.8rem; color:var(--text-muted); display:flex; justify-content:space-between;">
        <span>开场前基准价: <b id="open-twap" style="color:var(--text); font-family:monospace;">$--</b></span>
        <span>场次: <b id="round-time-str" style="color:var(--text); font-family:monospace;">--</b></span>
      </div>

      <div style="margin-top:14px; padding-top:12px; border-top:1px solid var(--card-border);">
        <div style="display:flex; justify-content:space-between; font-size:0.8rem; color:var(--text-muted);">
          <span>下一场开场前预测</span>
          <b id="upcoming-eta" style="font-family:monospace; color:#38bdf8;">--:--</b>
        </div>
        <div id="upcoming-dir" style="margin-top:6px; font-weight:800; font-size:1.05rem;">等待开场前数据</div>
        <div style="font-size:0.75rem; color:var(--text-muted); margin-top:4px;">
          分数 <b id="upcoming-score" style="color:var(--text); font-family:monospace;">--</b>
          · 置信度 <b id="upcoming-conf" style="color:var(--text);">--</b>
          · <span id="upcoming-time">--</span>
        </div>
      </div>
    </div>
    <div class="trade-col">
      <div class="card-title"><span>快买</span></div>
      <div class="trade-bal">
        <span>持仓 <b id="trade-port">--</b></span>
        <span>现金 <b id="trade-cash">--</b></span>
      </div>
      <div id="book-title" class="trade-market">当前场次</div>
      <div class="quick-grid" id="quick-book">
        <button type="button" id="buy-up" class="quick-buy up" disabled></button>
        <button type="button" id="buy-down" class="quick-buy down" disabled></button>
      </div>
      <div class="amt-row">
        <button type="button" id="amt-minus" class="step-btn">−</button>
        <input id="order-amount" value="5" inputmode="decimal" />
        <span class="amt-unit">$</span>
        <button type="button" id="amt-plus" class="step-btn">+</button>
      </div>
      <div class="amt-chips">
        <button type="button" data-add="1">+$1</button>
        <button type="button" data-add="5">+$5</button>
        <button type="button" data-add="10">+$10</button>
        <button type="button" data-add="100">+$100</button>
      </div>
      <div id="book-msg"></div>
    </div>
    </div>

    <!-- 信号雷达/评分分解 -->
    <div class="card">
      <div class="card-title" id="score-card-title">开场前多因子评分</div>
      <table class="score-table">
        <tbody>
          <tr>
            <td>Layer 1: 宏观趋势 (1h EMA-21)</td>
            <td class="score-val" id="score-macro">0.0</td>
          </tr>
          <tr>
            <td>Layer 2: 5m RSI 动能/超买超卖</td>
            <td class="score-val" id="score-rsi">0.0</td>
          </tr>
          <tr>
            <td>Layer 2: 5m 布林通道位置 (BB %)</td>
            <td class="score-val" id="score-bb">0.0</td>
          </tr>
          <tr>
            <td>Layer 2: 5m K线实体动量与放量</td>
            <td class="score-val" id="score-mom">0.0</td>
          </tr>
          <tr>
            <td>Layer 3: 开场前 60 秒斜率 (USD/s)</td>
            <td class="score-val" id="score-micro">0.0</td>
          </tr>
        </tbody>
      </table>
      <div style="font-size:0.75rem; color:var(--text-muted); margin-top:14px; line-height: 1.4;">
        * 开场前锁定。评分 &gt; +30 为 <b>UP</b>，&lt; -30 为 <b>DOWN</b>，中间为 <b>SKIP</b>。本局开始后不再改。
      </div>
    </div>

  </div>

  <!-- 第三排：历史场次结果流水表 -->
  <div class="card">
    <div class="history-bar">
      <div class="history-name">
        <b>历史场次</b>
        <span>北京时间</span>
      </div>
      <div class="history-stats">
        <b id="stat-accuracy" class="hit-rate">0.0%</b>
        <span>命中 <b id="stat-correct">0</b>/<b id="stat-called">0</b></span>
        <span>跳过 <b id="stat-skipped">0</b></span>
        <span>结算 <b id="stat-settled">0</b></span>
      </div>
      <button type="button" class="ghost-btn danger" id="clear-history">清空</button>
    </div>
    <div id="history-msg"></div>
    <div class="table-container">
      <table>
        <thead>
          <tr>
            <th>开盘</th>
            <th>预测</th>
            <th>置信度</th>
            <th>开盘价</th>
            <th>收盘价</th>
            <th>方向</th>
            <th>结果</th>
          </tr>
        </thead>
        <tbody id="history-rows">
          <tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding: 20px;">正在加载历史数据...</td></tr>
        </tbody>
      </table>
    </div>
    </div>

  <script>
    async function updateDashboard() {
      try {
        const res = await fetch('/api/status');
        if (res.status === 401) { location.reload(); return; }
        const data = await res.json();
        renderHistory(data.history);
        renderTraffic(data.traffic);
        
        // 顶部行情
        const bp = data.feed_stats.binance_price;
        if (bp > 0) document.getElementById('binance-price').innerText = '$ ' + bp.toLocaleString('en-US', {minimumFractionDigits: 2});
        
        const cp = data.chainlink.price;
        if (cp > 0) document.getElementById('chainlink-price').innerText = '$ ' + cp.toLocaleString('en-US', {minimumFractionDigits: 2});

        if (bp > 0 && cp > 0) {
          const diff = bp - cp;
          const diffPct = (diff / cp) * 100;
          document.getElementById('spread-val').innerText = (diff >= 0 ? '+' : '') + diff.toFixed(2) + ' USD';
          document.getElementById('spread-pct').innerText = '偏离 ' + (diffPct >= 0 ? '+' : '') + diffPct.toFixed(3) + '% (阈值 0.50%)';
        }

        // 胜率战绩
        const st = data.stats;
        const acc = document.getElementById('stat-accuracy');
        acc.innerText = st.accuracy_pct + '%';
        acc.style.color = st.called_rounds ? 'var(--up)' : 'var(--text-muted)';
        document.getElementById('stat-settled').innerText = st.settled_rounds;
        document.getElementById('stat-called').innerText = st.called_rounds || 0;
        document.getElementById('stat-correct').innerText = st.correct;
        document.getElementById('stat-skipped').innerText = st.skipped;

        // 渲染核心连接链路实时健康矩阵
        const conns = data.connections || [];
        if (conns.length > 0) {
          let cHtml = '';
          for (const c of conns) {
            let dotClass = 'dot-green';
            let rttColor = '#10b981';
            if (c.status === 'DISCONNECTED' || c.status === 'UNREACHABLE') {
              dotClass = 'dot-red';
              rttColor = '#ef4444';
            } else if (c.status === 'HIGH_LATENCY' || c.status === 'UNREADY' || c.status === 'DRIFTING' || c.status === 'CONNECTING') {
              dotClass = 'dot-yellow';
              rttColor = '#f59e0b';
            }
            const rttStr = c.rtt_ms > 0 ? c.rtt_ms.toFixed(1) + ' ms' : (c.id === 'clock_sync' ? c.rtt_ms.toFixed(1) + ' ms' : '-- ms');
            cHtml += `
            <div class="conn-card">
              <div class="conn-top">
                <div class="conn-name">
                  <span class="conn-dot ${dotClass}"></span>
                  <span>${c.name}</span>
                </div>
                <span class="conn-type">${c.type}</span>
              </div>
              <div style="display:flex; justify-content:space-between; align-items:baseline; margin-top:2px;">
                <div class="conn-rtt" style="color:${rttColor};">${rttStr}</div>
                <div style="font-size:0.75rem; font-weight:600; color:${rttColor};">${c.status_text}</div>
              </div>
              <div class="conn-endpoint" title="${c.endpoint}">🌐 ${c.endpoint}</div>
              <div class="conn-info">
                <span>${c.detail}</span>
                <span>${c.sub_detail}</span>
              </div>
            </div>`;
          }
          document.getElementById('conn-matrix').innerHTML = cHtml;
        }

        // 系统与连接
        document.getElementById('sys-uptime').innerText = data.system.uptime_str;
        document.getElementById('ws-status').innerHTML = data.feed_stats.connected ? '<span style="color:#10b981;">已连接</span>' : '<span style="color:#ef4444;">断开</span>';
        document.getElementById('cl-status').innerHTML = data.chainlink.price > 0 ? '<span style="color:#10b981;">正常</span>' : '<span style="color:#ef4444;">未就绪</span>';
        document.getElementById('ws-rate').innerText = data.feed_stats.msg_rate;
        document.getElementById('ws-kb').innerText = data.feed_stats.byte_rate_kb;
        document.getElementById('cl-age').innerText = data.chainlink.age_sec + 's';
        document.getElementById('cl-rtt').innerText = data.chainlink.rpc_rtt_ms + 'ms';
        document.getElementById('cl-rpc-node').innerText = data.chainlink.rpc_host || '自动选择中';
        
        // 硬件状态
        document.getElementById('sys-cpu').innerText = data.system.cpu_pct + '%';
        document.getElementById('bar-cpu').style.width = data.system.cpu_pct + '%';
        document.getElementById('sys-mem').innerText = data.system.mem_pct + '% (' + data.system.mem_used_mb + ' / ' + data.system.mem_total_mb + ' MB)';
        document.getElementById('bar-mem').style.width = data.system.mem_pct + '%';
        document.getElementById('sys-os').innerText = data.system.os;
        document.getElementById('net-in').innerText = data.system.net_in_rate_kb + ' KB/s';

        // 当前场次状态
        const cur = data.current_round;
        const phaseMap = {
          LOCKED: '开场前已锁定',
          NO_CALL: '本局无开场前预测',
          SETTLEMENT_WINDOW: '结算窗口 · 预测已锁定',
          INIT: '正在锁定'
        };
        document.getElementById('round-phase').innerText = phaseMap[cur.phase] || cur.phase || 'WAITING';
        document.getElementById('round-time-str').innerText = cur.open_time_str || '--:--';
        document.getElementById('open-twap').innerText = cur.opening_price > 0 ? '$ ' + cur.opening_price.toFixed(2) : '$ --';

        const rem = cur.time_remaining || 0;
        const mins = Math.floor(rem / 60);
        const secs = rem % 60;
        document.getElementById('round-timer').innerText = (mins < 10 ? '0' : '') + mins + ':' + (secs < 10 ? '0' : '') + secs;
        document.getElementById('round-timer-bar').style.width = ((rem / 300) * 100) + '%';

        // 决策 Badge
        const badge = document.getElementById('dir-badge');
        const dir = cur.predicted_dir;
        const lockNote = document.getElementById('dir-lock-note');
        if (dir === 'UP') {
          badge.className = 'decision-badge dir-UP';
          badge.innerText = '↑ 开场前看涨 (UP)';
          lockNote.innerText = '开场时刻已锁定，本局内不再改';
        } else if (dir === 'DOWN') {
          badge.className = 'decision-badge dir-DOWN';
          badge.innerText = '↓ 开场前看跌 (DOWN)';
          lockNote.innerText = '开场时刻已锁定，本局内不再改';
        } else if (dir === 'SKIP') {
          badge.className = 'decision-badge dir-SKIP';
          badge.innerText = '⏸ 开场前跳过 (SKIP)';
          lockNote.innerText = '开场时刻已锁定为观望，本局内不再改';
        } else {
          badge.className = 'decision-badge dir-WAIT';
          badge.innerText = '本局无开场前预测';
          lockNote.innerText = '进程在本局开场后才启动。请看下面的下一场预告';
        }

        document.getElementById('dir-confidence').innerText = dir ? (cur.confidence || 0).toFixed(1) + '%' : '--';
        document.getElementById('dir-score').innerText = dir ? ((cur.score >= 0 ? '+' : '') + (cur.score || 0).toFixed(1)) : '--';

        const up = data.upcoming_round || {};
        const upRem = up.time_until_open || 0;
        const upMin = Math.floor(upRem / 60);
        const upSec = upRem % 60;
        document.getElementById('upcoming-eta').innerText = '距开场 ' + (upMin < 10 ? '0' : '') + upMin + ':' + (upSec < 10 ? '0' : '') + upSec;
        const upDir = up.predicted_dir;
        const upEl = document.getElementById('upcoming-dir');
        if (upDir === 'UP') {
          upEl.style.color = 'var(--up)';
          upEl.innerText = '↑ 预告看涨 UP';
        } else if (upDir === 'DOWN') {
          upEl.style.color = 'var(--down)';
          upEl.innerText = '↓ 预告看跌 DOWN';
        } else if (upDir === 'SKIP') {
          upEl.style.color = 'var(--skip)';
          upEl.innerText = '⏸ 预告跳过 SKIP';
        } else {
          upEl.style.color = 'var(--text-muted)';
          upEl.innerText = '开场前数据还不够';
        }
        document.getElementById('upcoming-score').innerText = upDir ? ((up.score >= 0 ? '+' : '') + (up.score || 0).toFixed(1)) : '--';
        document.getElementById('upcoming-conf').innerText = upDir ? ((up.confidence || 0).toFixed(1) + '%') : '--';
        document.getElementById('upcoming-time').innerText = up.open_time_str || '--';

        // 有本场锁定预测时展示本场因子；否则展示下一场正在形成的开场前评分
        const scoreSource = dir ? cur : up;
        document.getElementById('score-card-title').innerText = dir ? '本场开场前评分（已锁定）' : '下一场开场前评分（尚未锁定）';
        const bd = scoreSource.breakdown || {};
        const fmtScore = (v) => (v > 0 ? '+' : '') + (v || 0).toFixed(1);
        document.getElementById('score-macro').innerText = fmtScore(bd.macro);
        document.getElementById('score-rsi').innerText = fmtScore(bd.rsi) + ' (RSI: ' + (bd.rsi_val || '--') + ')';
        document.getElementById('score-bb').innerText = fmtScore(bd.bb);
        document.getElementById('score-mom').innerText = fmtScore(bd.momentum);
        document.getElementById('score-micro').innerText = fmtScore(bd.micro) + (bd.slope_usd_s ? ' (' + bd.slope_usd_s + ' $/s)' : '');

      } catch (err) {
        console.error('Update error:', err);
        const rows = document.getElementById('history-rows');
        if (rows && rows.textContent.indexOf('正在加载') !== -1) {
          rows.innerHTML = '<tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding: 24px;">历史加载失败，请刷新页面</td></tr>';
        }
      }
    }

    function chinaClock(openTs) {
      const ts = Number(openTs);
      if (!Number.isFinite(ts) || ts <= 0) return '--';
      const parts = new Intl.DateTimeFormat('en-GB', {
        timeZone: 'Asia/Shanghai',
        month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
        hourCycle: 'h23'
      }).formatToParts(new Date(ts * 1000));
      const pick = (type) => parts.find((part) => part.type === type).value;
      return pick('month') + '-' + pick('day') + ' ' + pick('hour') + ':' + pick('minute') + ':' + pick('second');
    }

    function moneyText(value) {
      const n = Number(value);
      return Number.isFinite(n) && n !== 0 ? '$' + n.toFixed(2) : '--';
    }

    function renderHistory(history) {
      const rows = document.getElementById('history-rows');
      const hist = history || [];
      if (!hist.length) {
        rows.innerHTML = '<tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding: 24px;">暂无已保存场次</td></tr>';
        return;
      }
      let html = '';
      for (const r of hist) {
        let resTag = r.live
          ? '<span class="res-tag tag-pend">本场进行中</span>'
          : '<span class="res-tag tag-pend">进行中</span>';
        if (r.phase === 'SETTLED' && !r.actual_dir) {
          resTag = '<span class="res-tag tag-pend">无开场基准</span>';
        } else if (r.actual_dir) {
          if (!r.predicted_dir) {
            resTag = '<span class="res-tag tag-pend">无开场前预测</span>';
          } else if (r.predicted_dir === 'SKIP') {
            resTag = '<span class="res-tag tag-skip">避险跳过</span>';
          } else if (r.is_correct === 1 || r.is_correct === true) {
            resTag = '<span class="res-tag tag-win">✓ 命中</span>';
          } else {
            resTag = '<span class="res-tag tag-loss">✗ 失误</span>';
          }
        }
        const predStr = r.predicted_dir ? (r.predicted_dir === 'UP' ? '<b style="color:var(--up);">UP ↑</b>' : (r.predicted_dir === 'DOWN' ? '<b style="color:var(--down);">DOWN ↓</b>' : '<span style="color:var(--skip);">SKIP</span>')) : '--';
        const actStr = r.actual_dir ? (r.actual_dir === 'UP' ? '<b style="color:var(--up);">UP</b>' : '<b style="color:var(--down);">DOWN</b>') : '--';
        const when = chinaClock(r.open_ts);
        html += '<tr' + (r.live ? ' style="background:rgba(59,130,246,0.08);"' : '') + '>'
          + '<td>' + when + '</td>'
          + '<td>' + predStr + '</td>'
          + '<td>' + (Number(r.confidence) || 0).toFixed(1) + '%</td>'
          + '<td>' + moneyText(r.opening_price) + '</td>'
          + '<td>' + moneyText(r.closing_price) + '</td>'
          + '<td>' + actStr + '</td>'
          + '<td>' + resTag + '</td>'
          + '</tr>';
      }
      rows.innerHTML = html;
    }

    function renderTraffic(tr) {
      if (!tr) return;
      document.getElementById('tr-req').innerText = tr.http_requests;
      document.getElementById('tr-in').innerText = tr.http_in_kb;
      document.getElementById('tr-out').innerText = tr.http_out_kb;
      document.getElementById('tr-rpm').innerText = tr.http_req_per_min;
      document.getElementById('tr-out-rate').innerText = tr.http_out_kbps;
      document.getElementById('tr-bn').innerText = tr.binance_mb;
      document.getElementById('tr-bn-rate').innerText = tr.binance_kbps;
      const bars = tr.minutes || [];
      const max = Math.max(0.01, ...bars.map(x => (x.http_kb || 0) + (x.binance_kb || 0)));
      document.getElementById('tr-bars').innerHTML = bars.map(x => {
        const h = Math.max(2, ((x.http_kb + x.binance_kb) / max) * 22);
        return '<span title="' + x.label + ' ' + (x.http_kb + x.binance_kb).toFixed(2) + ' KB" style="height:' + h + 'px"></span>';
      }).join('');
    }

    let lastBook = null;

    function bestAsk(leg) {
      const asks = (leg && leg.asks) || [];
      return asks.length ? asks[asks.length - 1].price : null;
    }

    function centsText(price) {
      const cents = Math.round(Number(price) * 10000) / 100;
      return cents + '¢';
    }

    function paintQuote(el, side, price) {
      const amount = Number(document.getElementById('order-amount').value);
      const name = side === 'UP' ? 'Up' : 'Down';
      el.disabled = !price;
      el.dataset.side = side;
      if (price) el.dataset.price = price;
      else el.removeAttribute('data-price');
      let sub = '暂无卖单';
      if (price && amount > 0) {
        const shares = Math.floor(amount / Number(price) * 100) / 100;
        sub = '$' + amount.toFixed(2) + ' 约 ' + shares.toFixed(2) + ' 份';
      } else if (price) {
        sub = '请先填写下单金额';
      }
      el.innerHTML = '<div class="quick-top"><span class="quick-name">' + name + '</span><span class="quick-px">' + (price ? centsText(price) : '—') + '</span></div><div class="quick-sub">' + sub + '</div>';
    }

    function paintBook(data) {
      lastBook = data;
      paintQuote(document.getElementById('buy-up'), 'UP', data && data.ready ? bestAsk(data.up) : null);
      paintQuote(document.getElementById('buy-down'), 'DOWN', data && data.ready ? bestAsk(data.down) : null);
    }

    async function updateBook() {
      try {
        const res = await fetch('/api/book');
        const data = await res.json();
        document.getElementById('book-title').innerText = data.ready ? (data.question || data.slug) : (data.slug || '当前场次盘口');
        paintBook(data);
      } catch (err) {
        console.error(err);
      }
    }

    function renderAccount(data) {
      const view = document.getElementById('acct-view');
      const form = document.getElementById('wallet-form');
      const cash = data && data.connected && data.balance != null ? '$' + Number(data.balance).toFixed(2) : '--';
      const port = data && data.connected && data.portfolio_value != null ? '$' + Number(data.portfolio_value).toFixed(2) : '--';
      document.getElementById('trade-cash').innerText = cash;
      document.getElementById('trade-port').innerText = port;
      if (!data.connected) {
        view.style.display = 'none';
        form.style.display = 'block';
        return;
      }
      form.style.display = 'none';
      view.style.display = 'block';
      const total = (Number(data.balance || 0) + Number(data.portfolio_value || 0)).toFixed(4);
      view.innerHTML = ''
        + '<div>已连接。私钥和 API 凭证只加密存在本机，页面不再显示。</div>'
        + '<div>签名地址 <span class="secret">' + data.signer + '</span></div>'
        + '<div>中转钱包 <span class="secret">' + data.deposit_wallet + '</span> · ' + data.wallet_type + '</div>'
        + '<div style="margin-top:8px;">现金余额 <b>' + (data.balance == null ? '--' : data.balance) + '</b> pUSD · 持仓市值 <b>' + (data.portfolio_value == null ? '--' : data.portfolio_value) + '</b> · 合计 <b>' + total + '</b></div>'
        + (data.balance_error ? '<div class="sub-info">' + data.balance_error + '</div>' : '')
        + '<button type="button" id="wallet-disconnect" class="text-btn">断开钱包</button>';
      document.getElementById('wallet-disconnect').onclick = disconnectWallet;
    }

    async function updateAccount() {
      const res = await fetch('/api/account');
      if (!res.ok) return;
      renderAccount(await res.json());
    }

    async function disconnectWallet() {
      await fetch('/api/account/disconnect', {method:'POST'});
      updateAccount();
    }

    document.getElementById('wallet-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const msg = document.getElementById('acct-msg');
      msg.innerText = '正在连接 Polymarket...';
      const res = await fetch('/api/account/connect', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          private_key: document.getElementById('wallet-key').value,
          wallet: document.getElementById('wallet-funder').value
        })
      });
      const data = await res.json();
      if (!res.ok) {
        msg.innerText = data.error || '连接失败';
        return;
      }
      document.getElementById('wallet-key').value = '';
      document.getElementById('wallet-funder').value = '';
      msg.innerText = '已连接，私钥已加密保存';
      renderAccount(data);
    });

    let amountSaveTimer = null;
    function rememberAmount() {
      const amount = Number(document.getElementById('order-amount').value);
      if (!(amount > 0)) return;
      clearTimeout(amountSaveTimer);
      amountSaveTimer = setTimeout(() => {
        fetch('/api/settings', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({order_amount: amount})
        });
      }, 400);
    }
    function setAmount(next) {
      const value = Math.max(0, Math.round(Number(next) * 100) / 100);
      document.getElementById('order-amount').value = value ? String(value) : '';
      paintBook(lastBook);
      rememberAmount();
    }
    paintBook(null);
    document.getElementById('order-amount').addEventListener('input', () => {
      paintBook(lastBook);
      rememberAmount();
    });
    document.getElementById('amt-minus').addEventListener('click', () => setAmount(Number(document.getElementById('order-amount').value) - 1));
    document.getElementById('amt-plus').addEventListener('click', () => setAmount(Number(document.getElementById('order-amount').value) + 1));
    document.querySelectorAll('.amt-chips button').forEach((btn) => {
      btn.addEventListener('click', () => setAmount(Number(document.getElementById('order-amount').value) + Number(btn.dataset.add)));
    });
    document.getElementById('quick-book').addEventListener('click', onBookClick);
    async function onBookClick(ev) {
      const btn = ev.target.closest('button[data-price]');
      if (!btn || btn.disabled) return;
      const amount = document.getElementById('order-amount').value;
      const msg = document.getElementById('book-msg');
      msg.innerText = '正在下单...';
      const res = await fetch('/api/order', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({side: btn.dataset.side, price: btn.dataset.price, amount: amount})
      });
      const data = await res.json();
      msg.innerText = data.message || data.error || '没有返回';
    }

    const settingsModal = document.getElementById('settings-modal');
    document.getElementById('settings-btn').addEventListener('click', () => { settingsModal.hidden = false; });
    document.getElementById('settings-close').addEventListener('click', () => { settingsModal.hidden = true; });
    settingsModal.addEventListener('click', (ev) => { if (ev.target === settingsModal) settingsModal.hidden = true; });
    document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') settingsModal.hidden = true; });

    document.getElementById('clear-history').addEventListener('click', async () => {
      if (!confirm('清空已结算的历史场次？本场预测会保留。')) return;
      const btn = document.getElementById('clear-history');
      const msg = document.getElementById('history-msg');
      btn.disabled = true;
      msg.innerText = '正在清空...';
      try {
        const res = await fetch('/api/history/clear', {method:'POST'});
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          msg.innerText = data.error || '清空失败';
          return;
        }
        msg.innerText = '已清空已结算场次';
        updateDashboard();
      } catch (err) {
        msg.innerText = '清空失败，请重试';
      } finally {
        btn.disabled = false;
      }
    });

    document.getElementById('logout-btn').addEventListener('click', async () => {
      await fetch('/api/auth/logout', {method:'POST'});
      location.reload();
    });

    function showAuth(needsSetup) {
      document.getElementById('app-root').style.display = 'none';
      document.getElementById('auth-gate').style.display = 'flex';
      document.getElementById('auth-title').innerText = needsSetup ? '设置登录账号' : '登录';
      document.getElementById('auth-hint').innerText = needsSetup ? '还没有账号。设置用户名和密码后进入看板。' : '输入已经设置的用户名和密码。';
      document.getElementById('auth-submit').innerText = needsSetup ? '保存并进入' : '登录';
      document.getElementById('auth-form').dataset.mode = needsSetup ? 'setup' : 'login';
    }

    document.getElementById('auth-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const mode = ev.target.dataset.mode;
      const res = await fetch(mode === 'setup' ? '/api/auth/setup' : '/api/auth/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          username: document.getElementById('auth-user').value,
          password: document.getElementById('auth-pass').value
        })
      });
      const data = await res.json();
      if (!res.ok) {
        document.getElementById('auth-error').innerText = data.error || '失败';
        return;
      }
      location.reload();
    });

    async function boot() {
      const res = await fetch('/api/auth/status');
      const st = await res.json();
      if (!st.authenticated) {
        showAuth(!!st.needs_setup);
        return;
      }
      document.getElementById('app-root').style.display = 'block';
      document.getElementById('auth-user-label').innerText = st.username || '';
      const cfg = await fetch('/api/settings');
      if (cfg.ok) {
        const saved = await cfg.json();
        if (saved.order_amount > 0) {
          document.getElementById('order-amount').value = String(saved.order_amount);
          paintBook(lastBook);
        }
      }
      setInterval(updateDashboard, 1200);
      updateDashboard();
      setInterval(updateAccount, 5000);
      updateAccount();
      setInterval(updateBook, 2000);
      updateBook();
    }
    boot();
  </script>
</div>
</body>
</html>
"""

class QuantRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            body = HTML_DASHBOARD.encode("utf-8")
            self._send(200, body, "text/html; charset=utf-8")
            return
        if url.path == "/api/auth/status":
            self._send_json(200, self.server.auth.status(self._token()))
            return
        if not self._token_ok():
            return
        if url.path == "/api/status":
            self._send_json(200, self.server.app_context.get_full_status())
        elif url.path == "/api/account":
            try:
                self._send_json(200, self.server.polymarket.account_view(self._wrap_key()))
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
        elif url.path == "/api/settings":
            self._send_json(200, self.server.auth.get_settings())
        elif url.path == "/api/book":
            current = self.server.app_context.rounds.get_current()
            self._send_json(200, self.server.polymarket.book(current.get("open_ts")))
        else:
            self._send(404, b"Not Found", "text/plain")

    def do_POST(self):
        url = urlparse(self.path)
        try:
            payload = self._read_json()
        except Exception:
            self._send_json(400, {"error": "请求体不是 JSON"})
            return
        if url.path == "/api/auth/setup":
            self._auth_action("setup", payload)
            return
        if url.path == "/api/auth/login":
            self._auth_action("login", payload)
            return
        if url.path == "/api/auth/logout":
            self.server.auth.logout(self._token())
            self._send_json(200, {"ok": True}, clear_cookie=True)
            return
        if not self._token_ok():
            return
        try:
            if url.path == "/api/account/connect":
                view = self.server.polymarket.connect(
                    payload.get("private_key", ""),
                    self._wrap_key(),
                    payload.get("wallet"),
                )
                self._send_json(200, view)
            elif url.path == "/api/account/disconnect":
                self.server.polymarket.disconnect()
                self._send_json(200, {"connected": False})
            elif url.path == "/api/settings":
                saved = self.server.auth.save_settings(payload.get("order_amount"))
                self._send_json(200, saved)
            elif url.path == "/api/history/clear":
                self.server.app_context.rounds.clear_history()
                self._send_json(200, {"ok": True})
            elif url.path == "/api/order":
                result = self.server.polymarket.place_order(
                    self._wrap_key(),
                    payload.get("side"),
                    payload.get("price"),
                    payload.get("amount"),
                )
                self._send_json(200, result)
            else:
                self._send(404, b"Not Found", "text/plain")
        except Exception as exc:
            self._send_json(400, {"error": str(exc)})

    def _auth_action(self, kind, payload):
        try:
            if kind == "setup":
                token = self.server.auth.setup(payload.get("username", ""), payload.get("password", ""))
            else:
                token = self.server.auth.login(payload.get("username", ""), payload.get("password", ""))
        except Exception as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, {"ok": True}, token=token)

    def _token(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith("qm_session="):
                return part.split("=", 1)[1]
        return None

    def _wrap_key(self):
        return self.server.auth.wrap_key(self._token())

    def _token_ok(self):
        if self.server.auth.session(self._token()):
            return True
        self._send_json(401, {"error": "未登录"})
        return False

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 20000:
            raise ValueError("请求过大")
        raw = self.rfile.read(length) if length else b"{}"
        self._bytes_in = length
        return json.loads(raw.decode("utf-8") or "{}")

    def _send_json(self, code, obj, token=None, clear_cookie=False):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8", token=token, clear_cookie=clear_cookie)

    def _send(self, code, body: bytes, content_type: str, token=None, clear_cookie=False):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if token:
            self.send_header(
                "Set-Cookie",
                f"qm_session={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age=604800",
            )
        if clear_cookie:
            self.send_header("Set-Cookie", "qm_session=; HttpOnly; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)
        incoming = getattr(self, "_bytes_in", 0)
        self.server.traffic.record_http(incoming, len(body))


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class WebDashboardServer:
    def __init__(self, host: str, port: int, app_context, auth, traffic, polymarket):
        self.host = host
        self.port = port
        self.app_context = app_context
        self.auth = auth
        self.traffic = traffic
        self.polymarket = polymarket
        self.server = None
        self.thread = None

    def start(self):
        self.server = ThreadedHTTPServer((self.host, self.port), QuantRequestHandler)
        self.server.app_context = self.app_context
        self.server.auth = self.auth
        self.server.traffic = self.traffic
        self.server.polymarket = self.polymarket
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
