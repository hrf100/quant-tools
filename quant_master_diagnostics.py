#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import socket
import ssl
import json
import os
import subprocess
import urllib.request
import http.client
import base64

GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RED = "\033[91m"
MAGENTA = "\033[95m"
GRAY = "\033[90m"
RESET = "\033[0m"
BOLD = "\033[1m"

def print_banner():
    print(f"{BOLD}{CYAN}=================================================================================================={RESET}")
    print(f"{BOLD}          QUANT MASTER DIAGNOSTICS: 芬兰/欧洲量化服务器全链路超级体检与网关优化工具       {RESET}")
    print(f"{BOLD}{CYAN}=================================================================================================={RESET}")

# ==================== 模块 1：系统内核、BBR 与时钟校准审计 ====================
def audit_system_and_kernel():
    print(f"\n{BOLD}{MAGENTA}▶ 1. Linux 系统内核、BBR 加速与硬件时钟审计{RESET}")
    print("-" * 98)
    try:
        cc = subprocess.check_output(["sysctl", "-n", "net.ipv4.tcp_congestion_control"], text=True).strip()
    except Exception:
        cc = "未知/获取失败"

    try:
        qdisc = subprocess.check_output(["sysctl", "-n", "net.core.default_qdisc"], text=True).strip()
    except Exception:
        qdisc = "未知/获取失败"

    try:
        nofile = subprocess.check_output(["ulimit", "-n"], shell=True, text=True).strip()
        nofile_val = int(nofile)
    except Exception:
        nofile = "未知"
        nofile_val = 0
        
    print(f"  • TCP 拥塞控制算法 : {GREEN if cc == 'bbr' else YELLOW}{cc}{RESET} (目标: bbr)")
    print(f"  • 公平队列管理 (qdisc): {GREEN if qdisc in ['fq', 'fq_codel'] else YELLOW}{qdisc}{RESET} (目标: fq)")
    print(f"  • 最大文件描述符 (ulimit): {GREEN if nofile_val >= 65535 else RED}{nofile}{RESET} (高频量化建议 >= 65535)")
    
    # 实时与币安原子钟对齐
    try:
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection("data-api.binance.vision", 443, context=ctx, timeout=3)
        t_send = time.time() * 1000
        conn.request("GET", "/api/v3/time", headers={"Connection": "keep-alive"})
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        t_recv = time.time() * 1000
        server_ms = body["serverTime"]
        conn.close()
        
        rtt = t_recv - t_send
        offset = server_ms - ((t_send + t_recv) / 2.0)
        print(f"  • 本地原子钟时间偏移   : {GREEN if abs(offset) < 15 else RED}{offset:+.2f} ms{RESET} (基准 RTT: {rtt:.1f}ms)")
    except Exception as e:
        print(f"  • 交易所时钟对齐异常: {e}")

# ==================== 模块 2：Polymarket 合规与下单限制检测 ====================
def check_polymarket_geoblock():
    print(f"\n{BOLD}{MAGENTA}▶ 2. Polymarket 官方 API 合规白名单 & 下单资格检测 (/api/geoblock){RESET}")
    print("-" * 98)
    ctx = ssl.create_default_context()
    try:
        url = "https://polymarket.com/api/geoblock"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, context=ctx, timeout=4) as resp:
            data = json.loads(resp.read().decode())
            blocked = data.get("blocked", False)
            country = data.get("country", "Unknown")
            ip_addr = data.get("ip", "Unknown")
            
            status_str = f"{RED}已拦截 (Blocked: True){RESET}" if blocked else f"{GREEN}完全合规免封锁 (Blocked: False){RESET}"
            print(f"  • 探测出口 IP       : {BOLD}{ip_addr}{RESET}")
            print(f"  • 识别国家代码      : {BOLD}{country}{RESET}")
            print(f"  • 下单与交易状态    : {status_str}")
    except Exception as e:
        print(f"  • Geoblock 接口请求失败 (可能受 Cloudflare 拦截): {e}")

# ==================== 模块 3：四大数据平台连通性与底层 WebSocket/REST 测试 ====================
class SimpleWS:
    def __init__(self, host, port=443, path="/", timeout=4.0):
        self.host = host
        self.port = int(port)
        self.path = path
        self.timeout = timeout
        self.sock = None
        self.rtt = 9999.0

    def measure_ping(self):
        try:
            ctx = ssl.create_default_context()
            ip = socket.gethostbyname(self.host)
            raw = socket.create_connection((ip, self.port), timeout=self.timeout)
            self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
            key = base64.b64encode(os.urandom(16)).decode()
            req = f"GET {self.path} HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            self.sock.sendall(req.encode())
            resp = self.sock.recv(1024).decode(errors="ignore")
            if "101" not in resp:
                self.sock.close()
                return 9999.0
            
            # 发送标准 RFC 6455 Ping 帧 (0x89: FIN+Ping, 0x80: Masked, 长度0)
            t0 = time.perf_counter()
            self.sock.sendall(bytearray([0x89, 0x80]) + os.urandom(4))
            self.sock.settimeout(2.5)
            while True:
                hdr = self.sock.recv(2)
                if not hdr: break
                op = hdr[0] & 0x0F
                length = hdr[1] & 0x7F
                if length > 0: self.sock.recv(length)
                if op == 0x0A: # Pong 响应
                    self.rtt = (time.perf_counter() - t0) * 1000
                    break
            self.sock.close()
            return self.rtt
        except Exception:
            if self.sock: 
                try: self.sock.close()
                except: pass
            return 9999.0

def test_four_platforms():
    print(f"\n{BOLD}{MAGENTA}▶ 3. 四大核心数据平台实时连通性与底层 RTT 测试{RESET}")
    print("-" * 98)
    
    targets = [
        ("Polymarket 核心撮合引擎 (CLOB)", "ws-subscriptions-clob.polymarket.com", "/ws/market"),
        ("Polymarket 预言机实时总线 (Live-Data)", "ws-live-data.polymarket.com", "/"),
        ("Binance 现货极速行情专线 (Vision)", "data-stream.binance.vision", "/ws/btcusdt@aggTrade"),
        ("OKX 官方公共行情流 (OKX-WS)", "ws.okx.com:8443", "/ws/v5/public"),
    ]
    
    for name, host_port, path in targets:
        if ":" in host_port:
            host, port = host_port.split(":")
        else:
            host, port = host_port, 443
            
        ws = SimpleWS(host, port, path)
        rtt = ws.measure_ping()
        rtt_str = f"{GREEN}{rtt:6.1f} ms{RESET}" if rtt < 300 else f"{RED}超时/阻断({rtt:.0f}ms){RESET}"
        print(f"  • {name:<35} | 终结点: {host:<35} | 底层 RTT: {rtt_str}")

# ==================== 模块 4：关键目标路由追踪 (MTR 简化抽样) ====================
def trace_key_routes():
    print(f"\n{BOLD}{MAGENTA}▶ 4. 关键目标服务器路由出口审计 (TCP Traceroute 抽样){RESET}")
    print("-" * 98)
    
    hosts = [
        ("Polymarket 伦敦网关", "clob.polymarket.com"),
        ("Binance 接入网关", "data-stream.binance.vision"),
    ]
    
    for title, host in hosts:
        ip = ""
        try:
            ip = socket.gethostbyname(host)
            cmd = ["mtr", "-r", "-n", "-c", "2", "-T", "-P", "443", ip]
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
            lines = [line for line in out.splitlines() if "|--" in line]
            last_hop = lines[-1] if lines else "未知路由"
            print(f"  • {title:<22} ({ip}) -> 最终出口特征: {CYAN}{last_hop.strip()}{RESET}")
        except Exception:
            print(f"  • {title:<22} ({ip or host}) -> mtr 未安装或缺少 root 权限，无法执行 TCP Traceroute")

# ==================== 模块 5：实战智能建议 ====================
def print_pro_suggestions():
    print(f"\n{BOLD}{MAGENTA}▶ 5. 架构优化与高频量化实战建议补充{RESET}")
    print("-" * 98)
    print(f"  1. {BOLD}主价格发现源锁定{RESET} : 坚持使用 Binance 现货 `stream.binance.com` 或 `data-stream.binance.vision`。")
    print(f"  2. {BOLD}多源时间同步守护{RESET} : 云服务器虚拟化时钟易漂移，建议配置 chrony 守护进程对接 NTP 池。")
    print(f"  3. {BOLD}CLOB 长连接保活机制{RESET} : 维持 Polymarket CLOB 订阅时，切记每隔 10~15 秒向服务端发送一次 PING 帧，防止 Cloudflare 静默断流。")
    print(f"  4. {BOLD}订单池 Keep-Alive 策略{RESET} : 下单接口 (`POST /order`) 必须复用 HTTPS 长连接池，规避每次重新 TLS 握手的数十毫秒损耗。")
    print(f"{BOLD}{CYAN}=================================================================================================={RESET}\n")

def main():
    print_banner()
    audit_system_and_kernel()
    check_polymarket_geoblock()
    test_four_platforms()
    trace_key_routes()
    print_pro_suggestions()

if __name__ == "__main__":
    main()
