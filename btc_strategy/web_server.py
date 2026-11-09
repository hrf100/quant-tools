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
    .header { display: flex; justify-content: space-between; align-items: center; padding-bottom: 16px; border-bottom: 1px solid var(--card-border); margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
    .header h1 { font-size: 1.35rem; font-weight: 700; color: #60a5fa; display: flex; align-items: center; gap: 8px; }
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
  </style>
</head>
<body>

  <div class="header">
    <h1>
      <span>⚡ Polymarket BTC 5m 量化决策中心</span>
      <span class="badge badge-live" id="live-indicator">LIVE</span>
    </h1>
    <div style="font-size: 0.85rem; color: var(--text-muted);">
      系统运行: <b id="sys-uptime" style="color:var(--text); font-family:monospace;">00:00:00</b> |
      Binance WS: <span id="ws-status" style="font-weight:600;">--</span> |
      Chainlink: <span id="cl-status" style="font-weight:600;">--</span>
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

    <div class="card" style="border-top: 3px solid #10b981;">
      <div class="card-title">历史实盘预测胜率 (命中率)</div>
      <div class="price-val" id="stat-accuracy" style="color: var(--up);">0.0%</div>
      <div class="sub-info">
        已结算: <b id="stat-settled">0</b> 局 | 命中: <b id="stat-correct">0</b> | 跳过: <b id="stat-skipped">0</b>
      </div>
    </div>
  </div>

  <!-- 第二排：当前场次实时判定 + 评分细分 -->
  <div class="grid" style="grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));">
    
    <div class="card current-card">
      <div class="card-title">
        <span>当前场次方向判定 (5-MIN ROUND)</span>
        <span id="round-phase" style="font-size:0.75rem; color:#60a5fa; font-weight:700;">--</span>
      </div>
      
      <div style="text-align: center; margin: 12px 0;">
        <div id="dir-badge" class="decision-badge dir-WAIT">等待信号...</div>
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
        <span>开盘基准 TWAP: <b id="open-twap" style="color:var(--text); font-family:monospace;">$--</b></span>
        <span>场次: <b id="round-time-str" style="color:var(--text); font-family:monospace;">--</b></span>
      </div>
    </div>

    <!-- 信号雷达/评分分解 -->
    <div class="card">
      <div class="card-title">本局多因子评分分解 (Factor Breakdown)</div>
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
            <td>Layer 3: 开盘30s后微观斜率 (USD/s)</td>
            <td class="score-val" id="score-micro">0.0</td>
          </tr>
        </tbody>
      </table>
      <div style="font-size:0.75rem; color:var(--text-muted); margin-top:14px; line-height: 1.4;">
        * 规则：评分 > +30 预测 <b>UP</b>，评分 < -30 预测 <b>DOWN</b>，中间盲区自动 <b>SKIP</b> 避险。
      </div>
    </div>

    <!-- 系统设备与性能监控 -->
    <div class="card">
      <div class="card-title">服务器与系统硬件状态</div>
      <div style="font-size:0.85rem; margin-bottom:10px;">
        <div style="display:flex; justify-content:space-between;">
          <span style="color:var(--text-muted);">CPU 负载:</span>
          <b id="sys-cpu" style="font-family:monospace;">0%</b>
        </div>
        <div class="bar-wrap"><div id="bar-cpu" class="bar-fill" style="width:0%; background:#10b981;"></div></div>
      </div>

      <div style="font-size:0.85rem; margin-bottom:10px;">
        <div style="display:flex; justify-content:space-between;">
          <span style="color:var(--text-muted);">内存占用:</span>
          <b id="sys-mem" style="font-family:monospace;">0% (0 / 0 MB)</b>
        </div>
        <div class="bar-wrap"><div id="bar-mem" class="bar-fill" style="width:0%; background:#3b82f6;"></div></div>
      </div>

      <div style="font-size: 0.8rem; color:var(--text-muted); line-height: 1.6; margin-top:10px;">
        <div>服务器 OS: <span id="sys-os" style="color:var(--text);">--</span></div>
        <div>网络接收速率: <span id="net-in" style="color:var(--text); font-family:monospace;">0 KB/s</span></div>
        <div>Polygon RPC: <span id="cl-rpc-node" style="color:var(--text); font-family:monospace;">--</span></div>
      </div>
    </div>

  </div>

  <!-- 第三排：历史场次结果流水表 -->
  <div class="card">
    <div class="card-title">
      <span>历史场次判定与实际结算结果 (Recent Rounds)</span>
      <span style="font-size: 0.75rem; color: var(--text-muted);">自动持久化存储至 SQLite</span>
    </div>
    <div class="table-container">
      <table>
        <thead>
          <tr>
            <th>场次开盘 (UTC)</th>
            <th>策略预测</th>
            <th>置信度</th>
            <th>开盘基准价</th>
            <th>收盘结算价</th>
            <th>实际结算方向</th>
            <th>结果判定</th>
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
        const data = await res.json();
        
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
        document.getElementById('stat-accuracy').innerText = st.accuracy_pct + '%';
        document.getElementById('stat-settled').innerText = st.settled_rounds;
        document.getElementById('stat-correct').innerText = st.correct;
        document.getElementById('stat-skipped').innerText = st.skipped;

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
        document.getElementById('round-phase').innerText = cur.phase || 'WAITING';
        document.getElementById('round-time-str').innerText = cur.open_time_str || '--:--';
        document.getElementById('open-twap').innerText = cur.opening_price > 0 ? '$ ' + cur.opening_price.toFixed(2) : (bp > 0 ? '$ ' + bp.toFixed(2) + ' (采样中)' : '$ --');

        const rem = cur.time_remaining || 0;
        const mins = Math.floor(rem / 60);
        const secs = rem % 60;
        document.getElementById('round-timer').innerText = (mins < 10 ? '0' : '') + mins + ':' + (secs < 10 ? '0' : '') + secs;
        document.getElementById('round-timer-bar').style.width = ((rem / 300) * 100) + '%';

        // 决策 Badge
        const badge = document.getElementById('dir-badge');
        const dir = cur.predicted_dir;
        if (dir === 'UP') {
          badge.className = 'decision-badge dir-UP';
          badge.innerText = '↑ 判定看涨 (UP)';
        } else if (dir === 'DOWN') {
          badge.className = 'decision-badge dir-DOWN';
          badge.innerText = '↓ 判定看跌 (DOWN)';
        } else if (dir === 'SKIP') {
          badge.className = 'decision-badge dir-SKIP';
          badge.innerText = '⏸ 观望跳过 (SKIP)';
        } else {
          badge.className = 'decision-badge dir-WAIT';
          badge.innerText = '⏳ 分析计算中...';
        }

        document.getElementById('dir-confidence').innerText = (cur.confidence || 0).toFixed(1) + '%';
        document.getElementById('dir-score').innerText = (cur.score >= 0 ? '+' : '') + (cur.score || 0).toFixed(1);

        // 评分 breakdown
        const bd = cur.breakdown || {};
        const fmtScore = (v) => (v > 0 ? '+' : '') + (v || 0).toFixed(1);
        document.getElementById('score-macro').innerText = fmtScore(bd.macro);
        document.getElementById('score-rsi').innerText = fmtScore(bd.rsi) + ' (RSI: ' + (bd.rsi_val || '--') + ')';
        document.getElementById('score-bb').innerText = fmtScore(bd.bb);
        document.getElementById('score-mom').innerText = fmtScore(bd.momentum);
        document.getElementById('score-micro').innerText = fmtScore(bd.micro) + (bd.slope_usd_s ? ' (' + bd.slope_usd_s + ' $/s)' : '');

        // 历史流水表
        const hist = data.history || [];
        if (hist.length > 0) {
          let html = '';
          for (const r of hist) {
            let resTag = '<span class="res-tag tag-pend">进行中</span>';
            if (r.actual_dir) {
              if (r.predicted_dir === 'SKIP') {
                resTag = '<span class="res-tag tag-skip">避险跳过</span>';
              } else if (r.is_correct === 1 || r.is_correct === true) {
                resTag = '<span class="res-tag tag-win">✓ 命中</span>';
              } else {
                resTag = '<span class="res-tag tag-loss">✗ 失误</span>';
              }
            }
            const predStr = r.predicted_dir ? (r.predicted_dir === 'UP' ? '<b style="color:var(--up);">UP ↑</b>' : (r.predicted_dir === 'DOWN' ? '<b style="color:var(--down);">DOWN ↓</b>' : '<span style="color:var(--skip);">SKIP</span>')) : '--';
            const actStr = r.actual_dir ? (r.actual_dir === 'UP' ? '<b style="color:var(--up);">UP</b>' : '<b style="color:var(--down);">DOWN</b>') : '--';
            const openP = r.opening_price ? '$' + r.opening_price.toFixed(2) : '--';
            const closeP = r.closing_price ? '$' + r.closing_price.toFixed(2) : '--';

            html += `<tr>
              <td>${r.open_time_str || new Date(r.open_ts * 1000).toISOString().substr(11, 8)}</td>
              <td>${predStr}</td>
              <td>${(r.confidence || 0).toFixed(1)}%</td>
              <td>${openP}</td>
              <td>${closeP}</td>
              <td>${actStr}</td>
              <td>${resTag}</td>
            </tr>`;
          }
          document.getElementById('history-rows').innerHTML = html;
        } else {
          document.getElementById('history-rows').innerHTML = '<tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding: 24px;">暂无历史结算场次（首局正在进行中，本局 5 分钟倒计时结束后将自动归档并展示准确率）</td></tr>';
        }

      } catch (err) {
        console.error('Update error:', err);
      }
    }

    // 每 1.2 秒刷新一次状态
    setInterval(updateDashboard, 1200);
    updateDashboard();
  </script>
</body>
</html>
"""

class QuantRequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 静默控制台 HTTP 请求日志，避免刷屏
        pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/" or url.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_DASHBOARD.encode("utf-8"))
        elif url.path == "/api/status":
            ctx = self.server.app_context
            data = ctx.get_full_status()
            payload = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class WebDashboardServer:
    def __init__(self, host: str, port: int, app_context):
        self.host = host
        self.port = port
        self.app_context = app_context
        self.server = None
        self.thread = None

    def start(self):
        self.server = ThreadedHTTPServer((self.host, self.port), QuantRequestHandler)
        self.server.app_context = self.app_context
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
