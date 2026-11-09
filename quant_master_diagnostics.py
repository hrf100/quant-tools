#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════════════╗
║          QUANT MASTER DIAGNOSTICS  v2.0                                        ║
║          量化交易服务器全链路超级体检与网关优化工具                             ║
║          支持 Linux / macOS / Windows · 零外部依赖                             ║
╚══════════════════════════════════════════════════════════════════════════════════╝
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
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

# ─────────────────────────── 颜色与样式 ──────────────────────────────────────
# Windows 旧终端不支持 ANSI，自动降级
_COLOR_SUPPORT = sys.stdout.isatty() and (
    platform.system() != "Windows"
    or "WT_SESSION" in os.environ  # Windows Terminal
    or "ANSICON" in os.environ
)

def _c(code, text):
    return f"{code}{text}\033[0m" if _COLOR_SUPPORT else text

def G(t): return _c("\033[92m", t)   # 绿
def Y(t): return _c("\033[93m", t)   # 黄
def R(t): return _c("\033[91m", t)   # 红
def C(t): return _c("\033[96m", t)   # 青
def M(t): return _c("\033[95m", t)   # 品红
def Gr(t): return _c("\033[90m", t)  # 灰
def B(t): return _c("\033[1m", t)    # 粗体

SEP = "─" * 100

# ─────────────────────────── 操作系统嗅探 ────────────────────────────────────
OS = platform.system()   # "Linux" / "Darwin" / "Windows"

# ─────────────────────────── 全局结果收集 ────────────────────────────────────
# 格式: { "节点名": {"host": str, "ip": str, "rtt": float, "reachable": bool} }
RESULTS: dict = {}
RESULTS_LOCK = threading.Lock()

# 时钟全局补偿量（毫秒），由 audit_clock 写入，后续测试可用
CLOCK_OFFSET_MS: float = 0.0

# ══════════════════════════════════════════════════════════════════════════════
# 模块 0：横幅与参数
# ══════════════════════════════════════════════════════════════════════════════
def print_banner():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    hostname = socket.gethostname()
    os_ver = platform.platform()
    print()
    print(B(C("╔" + "═" * 98 + "╗")))
    print(B(C("║")) + B(f"  QUANT MASTER DIAGNOSTICS v2.0  —  量化服务器全链路超级体检工具".center(98)) + B(C("║")))
    print(B(C("║")) + f"  主机: {B(hostname)}  |  OS: {os_ver[:55]}  |  时间: {now}".ljust(98) + B(C("║")))
    print(B(C("╚" + "═" * 98 + "╝")))

def parse_args():
    args = sys.argv[1:]
    return {
        "no_trace":  "--no-trace"  in args,
        "fix_clock": "--fix-clock" in args,
    }

# ══════════════════════════════════════════════════════════════════════════════
# 模块 1：系统内核、网络栈与时钟审计（自适应跨平台）
# ══════════════════════════════════════════════════════════════════════════════
def _sysctl(key: str) -> str:
    try:
        return subprocess.check_output(["sysctl", "-n", key], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "N/A"

def _ulimit() -> str:
    try:
        if OS == "Windows":
            return "N/A (Windows)"
        return subprocess.check_output("ulimit -n", shell=True, text=True, stderr=subprocess.DEVNULL).strip()
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
        nofile = _ulimit()

        print(f"  {'TCP 拥塞控制算法':<24}: {G(cc) if cc == 'bbr' else Y(cc)}  (目标: bbr)")
        print(f"  {'队列调度 (qdisc)':<24}: {G(qdisc) if qdisc in ['fq','fq_codel'] else Y(qdisc)}  (目标: fq)")
        print(f"  {'Socket 接收缓冲 rmem':<24}: {G(rmem) if int(rmem or 0) >= 4194304 else Y(rmem)} bytes")
        print(f"  {'Socket 发送缓冲 wmem':<24}: {G(wmem) if int(wmem or 0) >= 4194304 else Y(wmem)} bytes")
        try:
            nf = int(nofile)
            print(f"  {'最大文件描述符':<24}: {G(nofile) if nf >= 65535 else R(nofile)}  (高频建议 >= 65535)")
        except Exception:
            print(f"  {'最大文件描述符':<24}: {Y(nofile)}")

        # 是否 root
        is_root = (os.geteuid() == 0)
        print(f"  {'当前权限':<24}: {G('root ✔') if is_root else Y('非 root — 路由追踪将受限')}")

    elif OS == "Darwin":
        cc = _sysctl("net.inet.tcp.cc_algorithm")
        print(f"  {'TCP 拥塞控制算法':<24}: {Y(cc)}  (macOS 默认 cubic，无需 bbr)")
        is_root = (os.geteuid() == 0)
        print(f"  {'当前权限':<24}: {G('root ✔') if is_root else Y('非 root')}")

    elif OS == "Windows":
        try:
            cc = subprocess.check_output(
                ["powershell", "-Command", "Get-NetTCPSetting | Select-Object -ExpandProperty CongestionProvider"],
                text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            cc = "N/A"
        print(f"  {'TCP 拥塞控制算法':<24}: {Y(cc)}  (Windows 环境，建议部署 Linux 量化节点)")

    else:
        print(f"  未识别的 OS: {OS}")

# ══════════════════════════════════════════════════════════════════════════════
# 模块 2：时钟审计与自动纠正
# ══════════════════════════════════════════════════════════════════════════════

# 多个时钟源，取中位数，提高可信度
CLOCK_SOURCES = [
    ("Binance Vision",  "data-api.binance.vision",  443, "/api/v3/time",   "serverTime"),
    ("OKX REST",        "www.okx.com",               443, "/api/v5/public/time", None),
]

def _fetch_server_time(host, port, path, key):
    """返回 (server_ms, rtt_ms)，失败返回 (None, None)"""
    try:
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=4)
        t0 = time.time()
        conn.request("GET", path, headers={"Connection": "close"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        t1 = time.time()
        conn.close()
        rtt = (t1 - t0) * 1000
        local_mid = ((t0 + t1) / 2.0) * 1000

        if key:
            server_ms = body.get(key)
        else:
            # OKX: {"data":[{"ts":"1234567890123"}]}
            server_ms = int(body["data"][0]["ts"])

        if server_ms is None:
            return None, None
        offset = server_ms - local_mid
        return server_ms, rtt, offset
    except Exception:
        return None, None, None

def _try_fix_clock(offset_ms: float):
    """尝试调用系统 NTP 客户端强制同步时钟"""
    abs_offset = abs(offset_ms)
    print(f"\n  {B('→ 检测到时钟偏移 ' + f'{offset_ms:+.1f} ms，尝试自动纠正...')}")

    if OS == "Linux":
        # 优先 chronyc，退而 ntpdate
        if shutil.which("chronyc"):
            ret = subprocess.run(["chronyc", "makestep"], capture_output=True, text=True)
            if ret.returncode == 0:
                print(f"  {G('✔ chronyc makestep 执行成功，系统时钟已强制同步！')}")
            else:
                print(f"  {R('✘ chronyc makestep 失败:')} {ret.stderr.strip()}")
        elif shutil.which("ntpdate"):
            ret = subprocess.run(["ntpdate", "-u", "pool.ntp.org"], capture_output=True, text=True)
            if ret.returncode == 0:
                print(f"  {G('✔ ntpdate 同步成功！')}")
            else:
                print(f"  {R('✘ ntpdate 失败:')} {ret.stderr.strip()}")
        elif shutil.which("timedatectl"):
            ret = subprocess.run(["timedatectl", "set-ntp", "true"], capture_output=True, text=True)
            if ret.returncode == 0:
                print(f"  {G('✔ timedatectl NTP 已启用！')}")
            else:
                print(f"  {Y('⚠ timedatectl 执行受限，请以 root 运行')}")
        else:
            print(f"  {Y('⚠ 未找到 chronyc/ntpdate/timedatectl，无法自动纠正时钟')}")
            print(f"  {Y('  建议执行: apt install chrony && chronyc makestep')}")

    elif OS == "Darwin":
        ret = subprocess.run(["sntp", "-sS", "pool.ntp.org"], capture_output=True, text=True)
        if ret.returncode == 0:
            print(f"  {G('✔ macOS sntp 同步成功！')}")
        else:
            print(f"  {Y('⚠ 请以 root 运行 sudo sntp -sS pool.ntp.org')}")

    elif OS == "Windows":
        ret = subprocess.run(["w32tm", "/resync", "/force"], capture_output=True, text=True)
        if ret.returncode == 0:
            print(f"  {G('✔ Windows w32tm 时钟同步成功！')}")
        else:
            print(f"  {Y('⚠ 请以管理员身份运行: w32tm /resync /force')}")

def _register_crontab_sync():
    """在 Linux 上注册每 5 分钟 chronyc makestep 的 crontab 任务"""
    if OS != "Linux":
        return
    if not shutil.which("crontab") or not shutil.which("chronyc"):
        return
    try:
        existing = subprocess.check_output(["crontab", "-l"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        existing = ""
    job = "*/5 * * * * /usr/bin/chronyc makestep > /dev/null 2>&1"
    if job not in existing:
        new_cron = existing.rstrip() + f"\n{job}\n"
        proc = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE, text=True)
        proc.communicate(input=new_cron)
        if proc.returncode == 0:
            print(f"  {G('✔ 已自动写入 crontab，每 5 分钟强制同步一次时钟')}")
        else:
            print(f"  {Y('⚠ crontab 写入失败，请手动添加:')}")
            print(f"  {Gr(job)}")
    else:
        print(f"  {G('✔ crontab 时钟同步任务已存在，无需重复添加')}")

def audit_clock(fix_clock=False):
    global CLOCK_OFFSET_MS
    print(f"\n{B(M('▶ 2. 交易所原子钟对齐 & 时钟漂移审计'))}")
    print(SEP)

    offsets = []
    for name, host, port, path, key in CLOCK_SOURCES:
        result = _fetch_server_time(host, port, path, key)
        server_ms, rtt, offset = result if len(result) == 3 else (*result, None)
        if offset is not None:
            status = G(f"{offset:+.2f} ms") if abs(offset) < 15 else R(f"{offset:+.2f} ms")
            print(f"  {'时钟源: ' + name:<30} RTT: {rtt:6.1f} ms  |  偏移: {status}")
            offsets.append(offset)
        else:
            print(f"  {'时钟源: ' + name:<30} {R('连接失败')}")

    if offsets:
        median_offset = sorted(offsets)[len(offsets) // 2]
        CLOCK_OFFSET_MS = median_offset
        summary_color = G if abs(median_offset) < 15 else (Y if abs(median_offset) < 100 else R)
        print(f"\n  {B('综合时钟偏移 (中位数):')} {summary_color(f'{median_offset:+.2f} ms')}")

        if abs(median_offset) < 15:
            print(f"  {G('✔ 时钟状态优秀，无需纠正')}")
        elif abs(median_offset) < 100:
            print(f"  {Y('⚠ 时钟偏移轻微，建议启用 NTP 守护进程')}")
            if fix_clock:
                _try_fix_clock(median_offset)
                _register_crontab_sync()
        else:
            print(f"  {R('✘ 时钟严重漂移！高频下单将被交易所拒绝！')}")
            if fix_clock:
                _try_fix_clock(median_offset)
                _register_crontab_sync()
            else:
                print(f"  {Y('  → 重新运行脚本并加参数 --fix-clock 可自动纠正')}")
    else:
        print(f"  {R('✘ 所有时钟源均不可达，无法评估时钟状态')}")

# ══════════════════════════════════════════════════════════════════════════════
# 模块 3：Polymarket 合规检测
# ══════════════════════════════════════════════════════════════════════════════
def check_polymarket_geoblock():
    print(f"\n{B(M('▶ 3. Polymarket 合规 & 下单白名单检测'))}")
    print(SEP)
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(
            "https://polymarket.com/api/geoblock",
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            blocked = data.get("blocked", False)
            country = data.get("country", "Unknown")
            ip_addr = data.get("ip", "Unknown")
            status = R("已拦截 Blocked=True  ✘ 此 IP 无法下单") if blocked else G("合规通过 Blocked=False ✔ 可正常交易")
            print(f"  {'出口公网 IP':<20}: {B(ip_addr)}")
            print(f"  {'识别国家代码':<20}: {B(country)}")
            print(f"  {'下单交易资格':<20}: {status}")
    except Exception as e:
        print(f"  {R('✘ Geoblock 接口不可达 (Cloudflare 拦截或网络故障):')} {e}")

# ══════════════════════════════════════════════════════════════════════════════
# 模块 4：全节点并发 WebSocket RTT 测量
# ══════════════════════════════════════════════════════════════════════════════

# ─── 完整节点清单 ─────────────────────────────────────────────────────────────
# 格式: (展示名称, host, port, ws_path, 分组)
ALL_ENDPOINTS = [
    # ── Polymarket ──────────────────────────────────────────────────
    ("Polymarket CLOB 撮合引擎",        "ws-subscriptions-clob.polymarket.com", 443, "/ws/market",             "Polymarket"),
    ("Polymarket Live-Data 预言机",     "ws-live-data.polymarket.com",          443, "/",                      "Polymarket"),

    # ── Binance 全球节点 ─────────────────────────────────────────────
    # 现货行情 (aggTrade)
    ("Binance 现货 stream (全球主线)",  "stream.binance.com",                   9443, "/ws/btcusdt@aggTrade",  "Binance"),
    ("Binance Vision (备线 CDN)",       "data-stream.binance.vision",           443,  "/ws/btcusdt@aggTrade",  "Binance"),
    # 现货 US 节点
    ("Binance.US 现货",                 "stream.binance.us",                    9443, "/ws/btcusdt@aggTrade",  "Binance"),
    # 期货节点
    ("Binance 期货 fstream (主线)",     "fstream.binance.com",                  443,  "/ws/btcusdt@aggTrade",  "Binance"),
    ("Binance 期货 dstream (欧洲 CDN)", "dstream.binance.com",                  443,  "/ws/btcusdt@aggTrade",  "Binance"),
    # 交割/期权
    ("Binance 交割 vstream",            "vstream.binance.com",                  9443, "/ws/BTCUSDT_240329@aggTrade", "Binance"),

    # ── OKX 全球节点 ─────────────────────────────────────────────────
    ("OKX 公共行情 (主线)",             "ws.okx.com",                           8443, "/ws/v5/public",          "OKX"),
    ("OKX 私有订单 (主线)",             "ws.okx.com",                           8443, "/ws/v5/private",         "OKX"),
    ("OKX AWS 节点 (低延迟专线)",       "wsaws.okx.com",                        8443, "/ws/v5/public",          "OKX"),
    ("OKX 爱尔兰节点 (欧洲专线)",      "wsie.okx.com",                         8443, "/ws/v5/public",          "OKX"),

    # ── Deribit (期权做市不可缺) ─────────────────────────────────────
    ("Deribit 主线 (期权/期货)",        "www.deribit.com",                      443,  "/ws/api/v2",             "Deribit"),
    ("Deribit 测试网",                  "test.deribit.com",                     443,  "/ws/api/v2",             "Deribit"),
]

class SimpleWS:
    def __init__(self, host, port=443, path="/", timeout=5.0):
        self.host = host
        self.port = int(port)
        self.path = path
        self.timeout = timeout
        self.sock = None

    def measure(self):
        """返回 (ip, rtt_ms) 或 (ip_or_host, 9999.0)"""
        ip = "未解析"
        try:
            ip = socket.gethostbyname(self.host)
            ctx = ssl.create_default_context()
            raw = socket.create_connection((ip, self.port), timeout=self.timeout)
            sock = ctx.wrap_socket(raw, server_hostname=self.host)
            key = base64.b64encode(os.urandom(16)).decode()
            handshake = (
                f"GET {self.path} HTTP/1.1\r\n"
                f"Host: {self.host}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(handshake.encode())
            resp = sock.recv(2048).decode(errors="ignore")
            if "101" not in resp:
                sock.close()
                return ip, 9999.0
            # RFC 6455 Ping: opcode=9, masked, payload=0
            t0 = time.perf_counter()
            sock.sendall(bytearray([0x89, 0x80]) + os.urandom(4))
            sock.settimeout(3.0)
            rtt = 9999.0
            while True:
                hdr = sock.recv(2)
                if len(hdr) < 2:
                    break
                op     = hdr[0] & 0x0F
                length = hdr[1] & 0x7F
                if length == 126:
                    sock.recv(2)
                elif length == 127:
                    sock.recv(8)
                if length > 0:
                    sock.recv(length)
                if op == 0x0A:   # Pong
                    rtt = (time.perf_counter() - t0) * 1000
                    break
            sock.close()
            return ip, rtt
        except Exception:
            return ip, 9999.0

def _probe_one(name, host, port, path, group, results):
    ws = SimpleWS(host, port, path)
    ip, rtt = ws.measure()
    with RESULTS_LOCK:
        results[name] = {"host": host, "ip": ip, "rtt": rtt, "reachable": rtt < 9000, "group": group}

def test_all_endpoints():
    print(f"\n{B(M('▶ 4. 全节点并发 WebSocket RTT 测量'))}")
    print(SEP)
    print(f"  {Gr('正在并发探测 ' + str(len(ALL_ENDPOINTS)) + ' 个节点，请稍候...')}")

    threads = []
    for (name, host, port, path, group) in ALL_ENDPOINTS:
        t = threading.Thread(target=_probe_one, args=(name, host, port, path, group, RESULTS))
        t.daemon = True
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=8)

    # 按分组输出，并标记最优节点
    current_group = None
    best: dict = {}

    # 先找各组最优
    for name, info in RESULTS.items():
        g = info["group"]
        if info["reachable"]:
            if g not in best or info["rtt"] < best[g]["rtt"]:
                best[g] = {**info, "name": name}

    for name, host, port, path, group in ALL_ENDPOINTS:
        info = RESULTS.get(name, {})
        if group != current_group:
            current_group = group
            print(f"\n  {B(C(f'[ {group} ]'))}")

        rtt  = info.get("rtt", 9999.0)
        ip   = info.get("ip", "N/A")
        reachable = info.get("reachable", False)
        is_best = best.get(group, {}).get("name") == name and reachable

        if reachable:
            if rtt < 50:
                rtt_str = G(f"{rtt:7.1f} ms")
            elif rtt < 150:
                rtt_str = G(f"{rtt:7.1f} ms")
            elif rtt < 300:
                rtt_str = Y(f"{rtt:7.1f} ms")
            else:
                rtt_str = Y(f"{rtt:7.1f} ms")
        else:
            rtt_str = R("  超时/阻断")

        best_tag = G("  ★ 最优") if is_best else ""
        print(f"    {'• ' + name:<42} {Gr(ip):<18} RTT: {rtt_str}{best_tag}")

    # 汇总推荐
    print(f"\n  {B('── 各平台最优接入节点推荐 ──')}")
    for group, info in best.items():
        print(f"    {B(group):<20} → {G(info['name'])}  ({info['host']})  RTT={info['rtt']:.1f}ms")

    return best  # 供路由追踪使用

# ══════════════════════════════════════════════════════════════════════════════
# 模块 5：路由追踪（对每个被测节点，自适应工具）
# ══════════════════════════════════════════════════════════════════════════════

def _detect_trace_tool():
    """返回可用的路由追踪工具名与参数模板，或 None"""
    if OS == "Linux":
        if shutil.which("mtr"):
            return "mtr"
        if shutil.which("traceroute"):
            return "traceroute"
    elif OS == "Darwin":
        if shutil.which("mtr"):
            return "mtr"
        if shutil.which("traceroute"):
            return "traceroute"
    elif OS == "Windows":
        return "tracert"
    return None

def _run_trace(tool, ip, host):
    """返回追踪摘要字符串"""
    try:
        if tool == "mtr":
            # -r 报告模式 -n 不反解 -c 2次采样 -T TCP -P 443
            cmd = ["mtr", "-r", "-n", "-c", "2", "-T", "-P", "443", ip]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=15)
            lines = [l for l in out.splitlines() if "|--" in l]
            if not lines:
                return Gr("无路由跳数记录")
            # 取最后 3 跳摘要
            last3 = lines[-3:] if len(lines) >= 3 else lines
            return "\n        ".join(Gr(l.strip()) for l in last3)

        elif tool == "traceroute":
            cmd = ["traceroute", "-n", "-T", "-p", "443", "-w", "1", "-q", "1", "-m", "20", ip]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=20)
            lines = [l for l in out.splitlines()[1:] if l.strip() and not l.strip().startswith("traceroute")]
            last3 = lines[-3:] if len(lines) >= 3 else lines
            return "\n        ".join(Gr(l.strip()) for l in last3)

        elif tool == "tracert":
            cmd = ["tracert", "-d", "-w", "1000", "-h", "20", ip]
            out = subprocess.check_output(cmd, text=True, errors="ignore", stderr=subprocess.DEVNULL, timeout=30)
            lines = [l for l in out.splitlines() if l.strip() and l.strip()[0].isdigit()]
            last3 = lines[-3:] if len(lines) >= 3 else lines
            return "\n        ".join(Gr(l.strip()) for l in last3)

    except subprocess.TimeoutExpired:
        return Y("追踪超时（目标不响应 ICMP/TCP 探针）")
    except Exception as e:
        return Y(f"追踪失败: {e}")
    return Gr("N/A")

def trace_all_endpoints(no_trace=False):
    print(f"\n{B(M('▶ 5. 全节点路由链路追踪（最后 3 跳）'))}")
    print(SEP)

    if no_trace:
        print(f"  {Y('⚠ 已跳过（--no-trace 参数）')}")
        return

    tool = _detect_trace_tool()
    if not tool:
        print(f"  {R('✘ 未找到路由追踪工具 (mtr/traceroute/tracert)，请先安装：apt install mtr')}")
        return

    if OS in ("Linux", "Darwin"):
        try:
            if os.geteuid() != 0:
                print(f"  {Y('⚠ 非 root 权限，mtr -T (TCP 模式) 可能失败，退化为 ICMP 模式')}")
        except Exception:
            pass

    print(f"  {Gr('使用工具: ' + tool + '  —  仅对可达节点执行追踪')}")

    for name, host, port, path, group in ALL_ENDPOINTS:
        info = RESULTS.get(name, {})
        if not info.get("reachable", False):
            print(f"\n  {Gr('· [跳过] ' + name + ' (不可达)')}")
            continue

        ip = info.get("ip", host)
        print(f"\n  {B('→ ' + name)}  {Gr('(' + ip + ')')}")
        summary = _run_trace(tool, ip, host)
        print(f"     {summary}")

# ══════════════════════════════════════════════════════════════════════════════
# 模块 6：实战优化建议（动态生成，根据检测结果）
# ══════════════════════════════════════════════════════════════════════════════
def print_pro_suggestions(best_nodes: dict):
    print(f"\n{B(M('▶ 6. 高频量化实战优化建议'))}")
    print(SEP)

    binance_best = best_nodes.get("Binance", {})
    okx_best     = best_nodes.get("OKX", {})
    poly_best    = best_nodes.get("Polymarket", {})

    print(f"  {B('① 最优行情接入节点（本次检测结果）')}")
    for g, info in best_nodes.items():
        print(f"     {g:<15}: {G(info['host'])}  (RTT {info['rtt']:.1f}ms)")

    print(f"\n  {B('② 时钟同步')}")
    if abs(CLOCK_OFFSET_MS) >= 15:
        print(f"     {R('当前偏移 ' + f'{CLOCK_OFFSET_MS:+.1f}ms，建议立即执行: sudo chronyc makestep')}")
    else:
        print(f"     {G('时钟状态良好 (' + f'{CLOCK_OFFSET_MS:+.1f}ms)，可选：每 5 分钟 crontab 守护')}")

    print(f"\n  {B('③ CLOB WebSocket 保活策略')}")
    print(f"     Polymarket CLOB 连接须每 {Y('10~15 秒')}发送一次 RFC 6455 Ping 控制帧，")
    print(f"     否则 Cloudflare 边缘节点会在 {R('30 秒')}后静默断流（无 RST，客户端不感知）。")

    print(f"\n  {B('④ HTTPS 长连接池复用')}")
    print(f"     下单/撤单接口 POST|DELETE /order 必须复用 {C('http.client.HTTPSConnection')} 长连接，")
    print(f"     每次重建 TLS 握手固定损耗 {Y('~150~250 ms')}（视节点距离），高频场景致命。")

    print(f"\n  {B('⑤ BBR + FQ 内核一键开启命令（Linux）')}")
    print(f"     {Gr('echo net.core.default_qdisc=fq >> /etc/sysctl.conf')}")
    print(f"     {Gr('echo net.ipv4.tcp_congestion_control=bbr >> /etc/sysctl.conf')}")
    print(f"     {Gr('sysctl -p')}")

    print(f"\n  {B('⑥ 单行部署命令（随时拉取最新版本）')}")
    print(f"     {C('curl -fsSL \"https://raw.githubusercontent.com/hrf100/quant-tools/main/quant_master_diagnostics.py?$(date +%s)\" | sudo python3')}")

    print()
    print(B(C("╔" + "═" * 98 + "╗")))
    print(B(C("║")) + B(" 诊断完成  —  QUANT MASTER DIAGNOSTICS v2.0".center(98)) + B(C("║")))
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
    best = test_all_endpoints()
    trace_all_endpoints(no_trace=args["no_trace"])
    print_pro_suggestions(best)

if __name__ == "__main__":
    main()
