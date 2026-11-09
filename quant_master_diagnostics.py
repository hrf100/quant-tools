#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════════╗
║          QUANT MASTER DIAGNOSTICS  v3.0                                        ║
║          Polymarket BTC 量化项目专用 · 全链路超级体检工具                      ║
║          支持 Linux / macOS / Windows · 零外部依赖                             ║
╚══════════════════════════════════════════════════════════════════════════════════╝
核心架构:
  Binance/OKX 现货价格（预测信号输入）
       ↓  比较价差 ↕
  Chainlink BTC/USD 预言机（Polygon）← Polymarket 实际结算依据
       ↓
  Polymarket CLOB 下单

用法:
  sudo python3 quant_master_diagnostics.py          # 完整检测（含路由追踪需 root）
  python3 quant_master_diagnostics.py --no-trace    # 跳过路由追踪（无需 root）
  python3 quant_master_diagnostics.py --fix-clock   # 检测并自动纠正时钟偏移
"""

import base64
import http.client
import json
import os
import platform
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

# ─────────────────────────── 颜色与样式 ──────────────────────────────────────
_COLOR_SUPPORT = sys.stdout.isatty() and (
    platform.system() != "Windows"
    or "WT_SESSION" in os.environ
    or "ANSICON" in os.environ
)
def _c(code, text): return f"{code}{text}\033[0m" if _COLOR_SUPPORT else text
def G(t):  return _c("\033[92m", t)
def Y(t):  return _c("\033[93m", t)
def R(t):  return _c("\033[91m", t)
def C(t):  return _c("\033[96m", t)
def M(t):  return _c("\033[95m", t)
def Gr(t): return _c("\033[90m", t)
def B(t):  return _c("\033[1m",  t)

SEP  = "─" * 100
SEP2 = "·" * 100

# ─────────────────────────── 全局状态 ────────────────────────────────────────
OS             = platform.system()
RESULTS        = {}          # WS 节点探测结果
RESULTS_LOCK   = threading.Lock()
CLOCK_OFFSET_MS: float = 0.0
BINANCE_BTC_PRICE: float = 0.0   # 实时 Binance BTC/USDT 价格
CHAINLINK_BTC_PRICE: float = 0.0 # 实时 Chainlink BTC/USD 价格

# ══════════════════════════════════════════════════════════════════════════════
# 模块 0：横幅与参数
# ══════════════════════════════════════════════════════════════════════════════
def print_banner():
    now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hostname = socket.gethostname()
    os_ver   = platform.platform()[:55]
    print()
    print(B(C("╔" + "═" * 98 + "╗")))
    print(B(C("║")) + B("  QUANT MASTER DIAGNOSTICS v3.0  —  Polymarket BTC 量化项目专用全链路体检".center(98)) + B(C("║")))
    print(B(C("║")) + f"  主机: {B(hostname)}  |  OS: {os_ver}  |  时间: {now}".ljust(98) + B(C("║")))
    print(B(C("╚" + "═" * 98 + "╝")))

def parse_args():
    args = sys.argv[1:]
    return {"no_trace": "--no-trace" in args, "fix_clock": "--fix-clock" in args}

# ══════════════════════════════════════════════════════════════════════════════
# 模块 1：系统内核、网络栈审计（跨平台自适应）
# ══════════════════════════════════════════════════════════════════════════════
def _sysctl(key):
    try:
        return subprocess.check_output(["sysctl", "-n", key], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "N/A"

def audit_system_and_kernel():
    print(f"\n{B(M('▶ 1. 系统内核 / 网络栈 / BBR 加速 审计'))}")
    print(SEP)
    if OS == "Linux":
        cc    = _sysctl("net.ipv4.tcp_congestion_control")
        qdisc = _sysctl("net.core.default_qdisc")
        rmem  = _sysctl("net.core.rmem_max")
        wmem  = _sysctl("net.core.wmem_max")
        try:
            nofile = subprocess.check_output("ulimit -n", shell=True, text=True, stderr=subprocess.DEVNULL).strip()
            nf = int(nofile)
        except Exception:
            nofile, nf = "N/A", 0
        print(f"  {'TCP 拥塞控制算法':<24}: {G(cc) if cc == 'bbr' else Y(cc)}  (目标: bbr)")
        print(f"  {'队列调度 (qdisc)':<24}: {G(qdisc) if qdisc in ['fq','fq_codel'] else Y(qdisc)}  (目标: fq)")
        print(f"  {'Socket rmem_max':<24}: {G(rmem) if int(rmem or 0) >= 4194304 else Y(rmem)} bytes")
        print(f"  {'Socket wmem_max':<24}: {G(wmem) if int(wmem or 0) >= 4194304 else Y(wmem)} bytes")
        print(f"  {'最大文件描述符':<24}: {G(nofile) if nf >= 65535 else R(nofile)}  (建议 >= 65535)")
        is_root = (os.geteuid() == 0)
        print(f"  {'当前权限':<24}: {G('root ✔') if is_root else Y('非 root — 路由追踪将受限')}")
    elif OS == "Darwin":
        cc = _sysctl("net.inet.tcp.cc_algorithm")
        print(f"  {'TCP 拥塞控制算法':<24}: {Y(cc)}  (macOS 默认 cubic)")
    elif OS == "Windows":
        try:
            cc = subprocess.check_output(
                ["powershell", "-Command", "Get-NetTCPSetting | Select-Object -ExpandProperty CongestionProvider"],
                text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            cc = "N/A"
        print(f"  {'TCP 拥塞控制算法':<24}: {Y(cc)}")

# ══════════════════════════════════════════════════════════════════════════════
# 模块 2：时钟审计与自动纠正
# ══════════════════════════════════════════════════════════════════════════════
CLOCK_SOURCES = [
    ("Binance Vision",  "data-api.binance.vision",  443, "/api/v3/time",        "serverTime", False),
    ("OKX REST",        "www.okx.com",               443, "/api/v5/public/time", None,         False),
]

def _fetch_server_time(host, port, path, key, _unused):
    try:
        ctx  = ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=4)
        t0   = time.time()
        conn.request("GET", path, headers={"Connection": "close"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        t1   = time.time()
        conn.close()
        rtt      = (t1 - t0) * 1000
        local_ms = ((t0 + t1) / 2.0) * 1000
        if key:
            server_ms = body.get(key)
        else:
            server_ms = int(body["data"][0]["ts"])
        if server_ms is None:
            return None, None, None
        return server_ms, rtt, server_ms - local_ms
    except Exception:
        return None, None, None

def _try_fix_clock(offset_ms):
    print(f"\n  {B('→ 检测到时钟偏移 ' + f'{offset_ms:+.1f} ms，尝试自动纠正...')}")
    if OS == "Linux":
        for tool, cmd in [("chronyc", ["chronyc", "makestep"]),
                          ("ntpdate", ["ntpdate", "-u", "pool.ntp.org"]),
                          ("timedatectl", ["timedatectl", "set-ntp", "true"])]:
            if shutil.which(tool):
                r = subprocess.run(cmd, capture_output=True, text=True)
                if r.returncode == 0:
                    print(f"  {G('✔ ' + tool + ' 时钟同步成功！')}")
                else:
                    print(f"  {Y('⚠ ' + tool + ' 执行失败: ' + r.stderr.strip())}")
                return
        print(f"  {Y('⚠ 未找到 chronyc/ntpdate，建议: apt install chrony && chronyc makestep')}")
    elif OS == "Darwin":
        r = subprocess.run(["sntp", "-sS", "pool.ntp.org"], capture_output=True, text=True)
        print(f"  {G('✔ sntp 同步成功') if r.returncode == 0 else Y('⚠ sudo sntp -sS pool.ntp.org')}")
    elif OS == "Windows":
        r = subprocess.run(["w32tm", "/resync", "/force"], capture_output=True, text=True)
        print(f"  {G('✔ w32tm 同步成功') if r.returncode == 0 else Y('⚠ 以管理员运行: w32tm /resync /force')}")

def _register_crontab_sync():
    if OS != "Linux" or not shutil.which("crontab") or not shutil.which("chronyc"):
        return
    try:
        existing = subprocess.check_output(["crontab", "-l"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        existing = ""
    job = "*/5 * * * * /usr/bin/chronyc makestep > /dev/null 2>&1"
    if job not in existing:
        proc = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE, text=True)
        proc.communicate(input=existing.rstrip() + f"\n{job}\n")
        print(f"  {G('✔ 已写入 crontab：每 5 分钟强制同步时钟')}")
    else:
        print(f"  {G('✔ crontab 时钟守护任务已存在')}")

def audit_clock(fix_clock=False):
    global CLOCK_OFFSET_MS
    print(f"\n{B(M('▶ 2. 交易所原子钟对齐 & 时钟漂移审计'))}")
    print(SEP)
    offsets = []
    for name, host, port, path, key, unused in CLOCK_SOURCES:
        server_ms, rtt, offset = _fetch_server_time(host, port, path, key, unused)
        if offset is not None:
            status = G(f"{offset:+.2f} ms") if abs(offset) < 15 else R(f"{offset:+.2f} ms")
            print(f"  {'时钟源: ' + name:<30} RTT: {rtt:6.1f} ms  |  偏移: {status}")
            offsets.append(offset)
        else:
            print(f"  {'时钟源: ' + name:<30} {R('连接失败')}")
    if offsets:
        med = sorted(offsets)[len(offsets) // 2]
        CLOCK_OFFSET_MS = med
        col = G if abs(med) < 15 else (Y if abs(med) < 100 else R)
        print(f"\n  {B('综合时钟偏移 (中位数):')} {col(f'{med:+.2f} ms')}")
        if abs(med) < 15:
            print(f"  {G('✔ 时钟状态优秀')}")
        elif abs(med) < 100:
            print(f"  {Y('⚠ 轻微漂移，建议 NTP 守护')}")
            if fix_clock: _try_fix_clock(med); _register_crontab_sync()
        else:
            print(f"  {R('✘ 时钟严重漂移！高频下单将被交易所拒绝！')}")
            if fix_clock: _try_fix_clock(med); _register_crontab_sync()
            else: print(f"  {Y('  → 加 --fix-clock 参数可自动纠正')}")

# ══════════════════════════════════════════════════════════════════════════════
# 模块 3：Polymarket 合规检测
# ══════════════════════════════════════════════════════════════════════════════
def check_polymarket_geoblock():
    print(f"\n{B(M('▶ 3. Polymarket 合规 & 下单白名单检测'))}")
    print(SEP)
    try:
        req = urllib.request.Request(
            "https://polymarket.com/api/geoblock",
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
        )
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=5) as resp:
            data    = json.loads(resp.read().decode())
            blocked = data.get("blocked", False)
            country = data.get("country", "Unknown")
            ip_addr = data.get("ip",      "Unknown")
            status  = R("已拦截 Blocked=True  ✘ 此 IP 无法下单") if blocked else G("合规通过 Blocked=False ✔ 可正常交易")
            print(f"  {'出口公网 IP':<20}: {B(ip_addr)}")
            print(f"  {'识别国家代码':<20}: {B(country)}")
            print(f"  {'下单交易资格':<20}: {status}")
    except Exception as e:
        print(f"  {R('✘ Geoblock 接口不可达:')} {e}")

# ══════════════════════════════════════════════════════════════════════════════
# 模块 4：Chainlink BTC/USD 预言机审计（Polymarket 结算核心）
# ══════════════════════════════════════════════════════════════════════════════

# Chainlink BTC/USD 在 Polygon 主网的聚合器合约地址
CHAINLINK_BTC_USD_POLYGON = "0xc907E116054Ad103354f2D350FD2514433D57F6F"

# latestRoundData() 函数选择器（ABI keccak256 前4字节）
LATEST_ROUND_DATA_SELECTOR = "0xfeaf968c"

# Polygon 公共 RPC 节点（供测试连通性与获取 Chainlink 价格）
POLYGON_RPC_NODES = [
    ("Polygon 官方 RPC",          "polygon-rpc.com",             443, "/",                "https"),
    ("Polygon Mainnet (Ankr)",    "rpc.ankr.com",                443, "/polygon",          "https"),
    ("Polygon Mainnet (1RPC)",    "1rpc.io",                     443, "/matic",             "https"),
    ("Polygon Bor (官方备用)",    "bor.polygon.technology",      443, "/",                 "https"),
    ("Polygon Alchemy 公共",      "polygon-mainnet.g.alchemy.com", 443, "/v2/demo",        "https"),
]

def _eth_call_rpc(rpc_url_host, rpc_url_path, to_addr, data_hex, timeout=4) -> str | None:
    """
    向 Polygon JSON-RPC 节点发送 eth_call，返回 result 字符串（hex），失败返回 None。
    纯 http.client 实现，零依赖。
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method":  "eth_call",
        "params":  [{"to": to_addr, "data": data_hex}, "latest"],
        "id":       1
    }).encode()
    try:
        ctx  = ssl.create_default_context()
        conn = http.client.HTTPSConnection(rpc_url_host, 443, context=ctx, timeout=timeout)
        conn.request("POST", rpc_url_path,
                     body=payload,
                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        conn.close()
        return body.get("result")
    except Exception:
        return None

def _decode_latest_round_data(hex_result: str):
    """
    解析 latestRoundData() 返回的 ABI 编码数据：
    返回 (roundId, answer_8dec, startedAt, updatedAt, answeredInRound)
    answer 除以 1e8 即 USD 价格
    """
    if not hex_result or hex_result == "0x":
        return None
    try:
        data = bytes.fromhex(hex_result[2:])
        # 5 个 int256/uint256，每个 32 字节
        if len(data) < 160:
            return None
        round_id        = int.from_bytes(data[0:32],   "big")
        answer          = int.from_bytes(data[32:64],  "big")
        started_at      = int.from_bytes(data[64:96],  "big")
        updated_at      = int.from_bytes(data[96:128], "big")
        answered_in_round = int.from_bytes(data[128:160], "big")
        # answer 是有符号整数
        if answer >= (1 << 255):
            answer -= (1 << 256)
        return round_id, answer / 1e8, started_at, updated_at, answered_in_round
    except Exception:
        return None

def _test_polygon_rpc_latency(name, host, port, path):
    """测试 RPC 节点延迟，使用 eth_blockNumber（最轻量级请求）"""
    payload = json.dumps({"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}).encode()
    try:
        ctx  = ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=5)
        t0   = time.time()
        conn.request("POST", path, body=payload,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        t1   = time.time()
        conn.close()
        rtt       = (t1 - t0) * 1000
        block_hex = body.get("result", "0x0")
        block_num = int(block_hex, 16)
        return rtt, block_num
    except Exception:
        return 9999.0, 0

def audit_chainlink_oracle():
    """
    核心模块：
    1. 测试所有 Polygon RPC 节点延迟，找出最优节点
    2. 通过最优节点读取 Chainlink BTC/USD latestRoundData
    3. 计算价格新鲜度、距触发更新的价差余量、与 Binance 实时价格的偏差
    """
    global CHAINLINK_BTC_PRICE

    print(f"\n{B(M('▶ 4. Chainlink BTC/USD 预言机审计（Polymarket 结算核心）'))}")
    print(SEP)
    print(f"  {Gr('说明: Polymarket BTC 价格市场通过 Chainlink 预言机结算，非 Binance 直接价格')}")
    print(f"  {Gr('合约: ' + CHAINLINK_BTC_USD_POLYGON + ' (Polygon 主网)')}\n")

    # ── 4.1 测试所有 Polygon RPC 节点 ────────────────────────────────────────
    print(f"  {B('4.1 Polygon RPC 节点延迟测试（Chainlink 读取依赖）')}")
    rpc_results = []
    for name, host, port, path, scheme in POLYGON_RPC_NODES:
        rtt, block = _test_polygon_rpc_latency(name, host, port, path)
        reachable = rtt < 9000 and block > 0
        rtt_str = G(f"{rtt:7.1f} ms") if rtt < 100 else (Y(f"{rtt:7.1f} ms") if rtt < 300 else R("   超时"))
        block_str = Gr(f"区块 #{block:,}") if block > 0 else Gr("区块 N/A")
        print(f"    {'• ' + name:<36} {rtt_str}  {block_str}")
        if reachable:
            rpc_results.append((rtt, name, host, port, path))

    if not rpc_results:
        print(f"\n  {R('✘ 所有 Polygon RPC 节点均不可达！无法读取 Chainlink 预言机数据。')}")
        print(f"  {Y('  → 检查服务器是否能访问 polygon-rpc.com，或配置专用 RPC（Alchemy/Infura）')}")
        return

    # 选最快的节点
    rpc_results.sort(key=lambda x: x[0])
    best_rtt, best_name, best_host, best_port, best_path = rpc_results[0]
    print(f"\n  {B('→ 最优 RPC 节点:')} {G(best_name)}  (RTT {best_rtt:.1f}ms)")

    # ── 4.2 读取 Chainlink latestRoundData ───────────────────────────────────
    print(f"\n  {B('4.2 Chainlink BTC/USD 实时预言机数据')}")
    t_query = time.time()
    hex_result = _eth_call_rpc(best_host, best_path,
                               CHAINLINK_BTC_USD_POLYGON,
                               LATEST_ROUND_DATA_SELECTOR)
    rpc_query_ms = (time.time() - t_query) * 1000

    decoded = _decode_latest_round_data(hex_result)
    if decoded is None:
        print(f"  {R('✘ 无法解析 Chainlink 返回数据')}  {Gr('(raw: ' + str(hex_result)[:40] + ')')}")
        return

    round_id, cl_price, started_at, updated_at, _ = decoded
    CHAINLINK_BTC_PRICE = cl_price
    now_ts     = time.time()
    age_sec    = now_ts - updated_at
    age_str    = f"{age_sec:.1f}s"
    # Chainlink 触发条件：价格偏差 > 0.5% 或距上次更新 > 27s（最大 1800s 心跳）
    # 实际 Polygon BTC/USD feed：deviation 0.5%，heartbeat 27s
    HEARTBEAT_S = 27
    stale = age_sec > HEARTBEAT_S * 3   # 超过 3 个心跳周期才算陈旧
    freshness_color = G if age_sec < HEARTBEAT_S else (Y if age_sec < HEARTBEAT_S * 3 else R)

    update_dt = datetime.fromtimestamp(updated_at).strftime("%H:%M:%S")
    print(f"    {'Round ID':<28}: {Gr(str(round_id))}")
    print(f"    {'BTC/USD 链上价格':<28}: {B(C(f'$ {cl_price:,.2f}'))}")
    print(f"    {'最后更新时间':<28}: {B(update_dt)}  {Gr('(UTC+本地时区)')}")
    print(f"    {'距上次更新':<28}: {freshness_color(age_str)}  (心跳周期 {HEARTBEAT_S}s，超过则触发更新)")
    print(f"    {'RPC 读取延迟':<28}: {G(f'{rpc_query_ms:.1f} ms') if rpc_query_ms < 150 else Y(f'{rpc_query_ms:.1f} ms')}")

    if stale:
        print(f"\n  {R('⚠ 警告：Chainlink 价格已陈旧 ' + age_str + '！可能存在价格发现风险。')}")

    # ── 4.3 Binance vs Chainlink 价差分析（套利信号核心）────────────────────
    print(f"\n  {B('4.3 Binance 现货 vs Chainlink 预言机 实时价差分析')}")
    global BINANCE_BTC_PRICE
    binance_price = _fetch_binance_btc_price()
    BINANCE_BTC_PRICE = binance_price

    if binance_price > 0 and cl_price > 0:
        spread_abs = binance_price - cl_price
        spread_pct = (spread_abs / cl_price) * 100.0
        # Chainlink 更新阈值是 0.5%
        CHAINLINK_UPDATE_THRESHOLD_PCT = 0.5
        margin_to_trigger = CHAINLINK_UPDATE_THRESHOLD_PCT - abs(spread_pct)

        print(f"    {'Binance BTC/USDT':<28}: {B(f'$ {binance_price:,.2f}')}")
        print(f"    {'Chainlink BTC/USD':<28}: {B(f'$ {cl_price:,.2f}')}")
        spread_color = G if abs(spread_pct) < 0.1 else (Y if abs(spread_pct) < 0.3 else R)
        print(f"    {'实时价差':<28}: {spread_color(f'{spread_abs:+.2f} USD  ({spread_pct:+.4f}%)')}")

        if margin_to_trigger > 0:
            print(f"    {'距触发链上更新余量':<28}: {Y(f'{margin_to_trigger:.4f}%')}  "
                  f"{Gr('(再偏离 ' + f'{margin_to_trigger:.4f}% 将触发 Chainlink 更新)')}")
        else:
            print(f"    {'链上更新状态':<28}: {R('⚡ 价差已超过阈值！Chainlink 正在或即将触发更新！')}")
            print(f"    {Gr('  → 此时 Polymarket 结算价格即将跳变，高风险时段！')}")

        # 交易策略提示
        print(f"\n  {B('4.4 Polymarket BTC 分钟涨跌结算风险评估')}")
        print(f"    {Gr(SEP2[:60])}")
        if abs(spread_pct) >= CHAINLINK_UPDATE_THRESHOLD_PCT:
            print(f"    {R('🔴 高风险')}：Chainlink 即将更新价格，")
            print(f"       结算价格将向 Binance 现货价格靠拢，当前链上价格 {R('不可信赖')}。")
        elif abs(spread_pct) >= 0.3:
            print(f"    {Y('🟡 中风险')}：价差接近阈值，密切监控 Chainlink 更新时机。")
        else:
            print(f"    {G('🟢 低风险')}：Binance 与 Chainlink 价差在正常范围内。")

        print(f"    {Gr(SEP2[:60])}")
        print(f"\n    {B('结算逻辑说明')}:")
        print(f"    • Polymarket 以 {C('Chainlink 链上价格')} 结算，不以 Binance 现货直接结算")
        print(f"    • Chainlink 触发条件: 价差 > {Y('0.5%')} 或心跳超时 ({Y('27s')})")
        print(f"    • 临近整点结算时，务必观察 Chainlink 是否已更新到最新价格")
        print(f"    • {Y('建议')}：策略中使用 Chainlink 价格作为结算基准，Binance 作为方向信号")
    else:
        print(f"  {Y('⚠ 无法同时获取 Binance 与 Chainlink 价格，跳过价差分析')}")

def _fetch_binance_btc_price() -> float:
    """获取 Binance BTC/USDT 实时价格"""
    try:
        ctx  = ssl.create_default_context()
        conn = http.client.HTTPSConnection("data-api.binance.vision", 443, context=ctx, timeout=4)
        conn.request("GET", "/api/v3/ticker/price?symbol=BTCUSDT",
                     headers={"Connection": "close"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return float(data.get("price", 0))
    except Exception:
        return 0.0

# ══════════════════════════════════════════════════════════════════════════════
# 模块 5：全节点并发 WebSocket RTT 测量
# ══════════════════════════════════════════════════════════════════════════════
ALL_ENDPOINTS = [
    # ── Polymarket ──────────────────────────────────────────────────────────
    ("Polymarket CLOB 撮合引擎",        "ws-subscriptions-clob.polymarket.com", 443,  "/ws/market",             "Polymarket"),
    ("Polymarket Live-Data 预言机",     "ws-live-data.polymarket.com",          443,  "/",                      "Polymarket"),

    # ── Binance ─────────────────────────────────────────────────────────────
    ("Binance 现货 stream (全球主线)",  "stream.binance.com",                   9443, "/ws/btcusdt@aggTrade",   "Binance"),
    ("Binance Vision (备线 CDN)",       "data-stream.binance.vision",           443,  "/ws/btcusdt@aggTrade",   "Binance"),
    ("Binance.US 现货",                 "stream.binance.us",                    9443, "/ws/btcusdt@aggTrade",   "Binance"),
    ("Binance 期货 fstream",            "fstream.binance.com",                  443,  "/ws/btcusdt@aggTrade",   "Binance"),
    ("Binance 期货 dstream (欧洲 CDN)", "dstream.binance.com",                  443,  "/ws/btcusdt@aggTrade",   "Binance"),
    ("Binance 交割 vstream",            "vstream.binance.com",                  9443, "/ws/BTCUSDT_240329@aggTrade", "Binance"),

    # ── OKX ─────────────────────────────────────────────────────────────────
    ("OKX 公共行情 (主线)",             "ws.okx.com",                           8443, "/ws/v5/public",          "OKX"),
    ("OKX AWS 专线",                    "wsaws.okx.com",                        8443, "/ws/v5/public",          "OKX"),
    ("OKX 爱尔兰 (欧洲专线)",          "wsie.okx.com",                         8443, "/ws/v5/public",          "OKX"),

    # ── Deribit ─────────────────────────────────────────────────────────────
    ("Deribit 主线 (期权/期货)",        "www.deribit.com",                      443,  "/ws/api/v2",             "Deribit"),
    ("Deribit 测试网",                  "test.deribit.com",                     443,  "/ws/api/v2",             "Deribit"),

    # ── Polygon RPC（Chainlink 结算链）──────────────────────────────────────
    ("Polygon RPC 官方",                "polygon-rpc.com",                      443,  "/",                      "Polygon/Chainlink"),
    ("Polygon Ankr RPC",                "rpc.ankr.com",                         443,  "/polygon",               "Polygon/Chainlink"),
    ("Polygon 1RPC",                    "1rpc.io",                              443,  "/matic",                 "Polygon/Chainlink"),
]

class SimpleWS:
    def __init__(self, host, port=443, path="/", timeout=5.0):
        self.host = host; self.port = int(port)
        self.path = path; self.timeout = timeout

    def measure(self):
        ip = "未解析"
        try:
            ip  = socket.gethostbyname(self.host)
            ctx = ssl.create_default_context()
            raw = socket.create_connection((ip, self.port), timeout=self.timeout)
            sock = ctx.wrap_socket(raw, server_hostname=self.host)
            key  = base64.b64encode(os.urandom(16)).decode()
            hs   = (f"GET {self.path} HTTP/1.1\r\nHost: {self.host}\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
            sock.sendall(hs.encode())
            resp = sock.recv(2048).decode(errors="ignore")
            if "101" not in resp:
                sock.close(); return ip, 9999.0
            t0 = time.perf_counter()
            sock.sendall(bytearray([0x89, 0x80]) + os.urandom(4))
            sock.settimeout(3.0)
            rtt = 9999.0
            while True:
                hdr = sock.recv(2)
                if len(hdr) < 2: break
                op = hdr[0] & 0x0F; length = hdr[1] & 0x7F
                if length == 126: sock.recv(2)
                elif length == 127: sock.recv(8)
                if length > 0: sock.recv(length)
                if op == 0x0A:
                    rtt = (time.perf_counter() - t0) * 1000; break
            sock.close(); return ip, rtt
        except Exception:
            return ip, 9999.0

def _probe_one(name, host, port, path, group, results):
    ip, rtt = SimpleWS(host, port, path).measure()
    with RESULTS_LOCK:
        results[name] = {"host": host, "ip": ip, "rtt": rtt,
                         "reachable": rtt < 9000, "group": group}

def test_all_endpoints():
    print(f"\n{B(M('▶ 5. 全节点并发 WebSocket / RPC RTT 测量'))}")
    print(SEP)
    print(f"  {Gr('正在并发探测 ' + str(len(ALL_ENDPOINTS)) + ' 个节点（含 Polygon RPC），约 5~8 秒...')}")

    threads = [threading.Thread(target=_probe_one, args=(n, h, p, pa, g, RESULTS), daemon=True)
               for n, h, p, pa, g in ALL_ENDPOINTS]
    for t in threads: t.start()
    for t in threads: t.join(timeout=8)

    best = {}
    for name, info in RESULTS.items():
        g = info["group"]
        if info["reachable"] and (g not in best or info["rtt"] < best[g]["rtt"]):
            best[g] = {**info, "name": name}

    current_group = None
    for name, host, port, path, group in ALL_ENDPOINTS:
        info = RESULTS.get(name, {})
        if group != current_group:
            current_group = group
            is_resolution_chain = "Polygon" in group
            tag = C(f"[ {group} ]") if not is_resolution_chain else M(f"[ {group} ]  ← Polymarket 结算链")
            print(f"\n  {B(tag)}")

        rtt       = info.get("rtt", 9999.0)
        ip        = info.get("ip", "N/A")
        reachable = info.get("reachable", False)
        is_best   = best.get(group, {}).get("name") == name and reachable

        if reachable:
            rtt_str = G(f"{rtt:7.1f} ms") if rtt < 150 else Y(f"{rtt:7.1f} ms")
        else:
            rtt_str = R("  超时/阻断")
        best_tag = G("  ★ 最优") if is_best else ""
        print(f"    {'• ' + name:<42} {Gr(ip):<18} RTT: {rtt_str}{best_tag}")

    print(f"\n  {B('── 各平台最优接入节点 ──')}")
    for group, info in best.items():
        print(f"    {B(group):<20} → {G(info['name'])}  ({info['host']})  RTT={info['rtt']:.1f}ms")

    return best

# ══════════════════════════════════════════════════════════════════════════════
# 模块 6：全节点路由追踪
# ══════════════════════════════════════════════════════════════════════════════
def _detect_trace_tool():
    if OS in ("Linux", "Darwin"):
        return "mtr" if shutil.which("mtr") else ("traceroute" if shutil.which("traceroute") else None)
    elif OS == "Windows":
        return "tracert"
    return None

def _run_trace(tool, ip):
    try:
        if tool == "mtr":
            out   = subprocess.check_output(["mtr", "-r", "-n", "-c", "2", "-T", "-P", "443", ip],
                                            text=True, stderr=subprocess.DEVNULL, timeout=15)
            lines = [l for l in out.splitlines() if "|--" in l]
            last3 = lines[-3:] if len(lines) >= 3 else lines
            return "\n        ".join(Gr(l.strip()) for l in last3)
        elif tool == "traceroute":
            out   = subprocess.check_output(["traceroute", "-n", "-T", "-p", "443", "-w", "1", "-q", "1", "-m", "20", ip],
                                            text=True, stderr=subprocess.DEVNULL, timeout=20)
            lines = [l for l in out.splitlines()[1:] if l.strip()]
            last3 = lines[-3:] if len(lines) >= 3 else lines
            return "\n        ".join(Gr(l.strip()) for l in last3)
        elif tool == "tracert":
            out   = subprocess.check_output(["tracert", "-d", "-w", "500", "-h", "20", ip],
                                            text=True, errors="ignore", stderr=subprocess.DEVNULL, timeout=30)
            lines = [l for l in out.splitlines() if l.strip() and l.strip()[0].isdigit()]
            last3 = lines[-3:] if len(lines) >= 3 else lines
            return "\n        ".join(Gr(l.strip()) for l in last3)
    except subprocess.TimeoutExpired:
        return Y("追踪超时")
    except Exception as e:
        return Y(f"追踪失败: {e}")

def trace_all_endpoints(no_trace=False):
    print(f"\n{B(M('▶ 6. 全节点路由链路追踪（最后 3 跳）'))}")
    print(SEP)
    if no_trace:
        print(f"  {Y('⚠ 已跳过（--no-trace 参数）')}"); return
    tool = _detect_trace_tool()
    if not tool:
        print(f"  {R('✘ 未找到 mtr/traceroute/tracert，请安装: apt install mtr')}"); return
    if OS in ("Linux", "Darwin"):
        try:
            if os.geteuid() != 0:
                print(f"  {Y('⚠ 非 root，mtr TCP 模式可能退化为 ICMP')}")
        except Exception:
            pass
    print(f"  {Gr('工具: ' + tool)}")
    for name, host, port, path, group in ALL_ENDPOINTS:
        info = RESULTS.get(name, {})
        if not info.get("reachable", False):
            print(f"\n  {Gr('· [跳过] ' + name + ' (不可达)')}"); continue
        ip = info.get("ip", host)
        print(f"\n  {B('→ ' + name)}  {Gr('(' + ip + ')')}")
        print(f"     {_run_trace(tool, ip)}")

# ══════════════════════════════════════════════════════════════════════════════
# 模块 7：综合优化建议（动态生成）
# ══════════════════════════════════════════════════════════════════════════════
def print_pro_suggestions(best_nodes: dict):
    print(f"\n{B(M('▶ 7. Polymarket BTC 量化项目实战优化建议'))}")
    print(SEP)

    print(f"  {B('① 结算架构（最重要）')}")
    print(f"     {C('Polymarket BTC 结算依据 = Chainlink BTC/USD (Polygon), 不是 Binance 实时价格')}")
    print(f"     推荐双轨监控:")
    print(f"       - {G('Binance aggTrade WS')}  → 方向信号（快，毫秒级）")
    print(f"       - {G('Polygon RPC eth_call')} → 轮询 Chainlink 链上价格（建议 1~3s 间隔）")

    print(f"\n  {B('② 临近结算的高危时段策略')}")
    print(f"     结算前 {Y('30s')} 内：")
    print(f"       1. 检查 Chainlink updated_at 时间戳，确认价格是否是最新 round")
    print(f"       2. 计算 |Binance价 - Chainlink价| / Chainlink价，")
    print(f"          若 > {Y('0.3%')} 应警惕价格即将跳变")
    print(f"       3. 若 Chainlink 超过 {Y('27s')} 未更新 + 价差 > 0.5%，结算价=下一个 round，")
    print(f"          {R('极端情况下价格跳变可达数百美元！')}")

    print(f"\n  {B('③ 最优节点（本次测量结果）')}")
    for g, info in best_nodes.items():
        print(f"     {g:<22}: {G(info['host'])}  ({info['rtt']:.1f}ms)")

    print(f"\n  {B('④ Polygon RPC 轮询 Chainlink 参考代码（零依赖）')}")
    print(f"     {Gr('''import http.client, json, ssl, struct''')}")
    print(f"     {Gr('''conn = http.client.HTTPSConnection(\"polygon-rpc.com\", 443)''')}")
    print(f"     {Gr('''payload = json.dumps({\"jsonrpc\":\"2.0\",\"method\":\"eth_call\",''')}")
    print(f"     {Gr('''  \"params\":[{\"to\":\"0xc907E116054Ad103354f2D350FD2514433D57F6F\",''')}")
    print(f"     {Gr('''  \"data\":\"0xfeaf968c\"},\"latest\"],\"id\":1}).encode()''')}")
    print(f"     {Gr('''# result[32:64] = answer bytes, divide by 1e8 = USD price''')}")

    print(f"\n  {B('⑤ CLOB 保活 & 连接池')}")
    print(f"     - Polymarket CLOB WS 每 {Y('10~15s')} 发送 Ping，防 Cloudflare 30s 静默断流")
    print(f"     - 下单接口复用 {C('http.client.HTTPSConnection')} 长连接，省 {Y('~200ms')} TLS 握手")

    print(f"\n  {B('⑥ 一行命令（始终最新版本）')}")
    print(f"     {C('curl -fsSL \"https://raw.githubusercontent.com/hrf100/quant-tools/main/quant_master_diagnostics.py?$(date +%s)\" | sudo python3')}")

    print()
    print(B(C("╔" + "═" * 98 + "╗")))
    print(B(C("║")) + B(" 诊断完成  —  QUANT MASTER DIAGNOSTICS v3.0  |  github.com/hrf100/quant-tools".center(98)) + B(C("║")))
    print(B(C("╚" + "═" * 98 + "╝")))
    print()

# ══════════════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════════════
def main():
    args = parse_args()
    print_banner()
    audit_system_and_kernel()
    audit_clock(fix_clock=args["fix_clock"])
    check_polymarket_geoblock()
    audit_chainlink_oracle()           # ← 新增 Chainlink 核心模块
    best = test_all_endpoints()
    trace_all_endpoints(no_trace=args["no_trace"])
    print_pro_suggestions(best)

if __name__ == "__main__":
    main()
