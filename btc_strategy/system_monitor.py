# btc_strategy/system_monitor.py
"""
系统与设备状态监控模块：
- CPU / 内存占用（跨平台探测，优先系统指令/ /proc，零第三方强制依赖）
- 网络流量与速率统计
- 服务运行时长与心跳
- 时钟偏移状态
"""

import os
import platform
import subprocess
import time
import socket
import ssl
import json
import http.client
import threading

OS = platform.system()

class SystemMonitor:
    def __init__(self, start_time: float = None):
        self.start_time = start_time or time.time()
        self._last_net_check = time.time()
        self._last_bytes_in = 0
        self._last_bytes_out = 0
        self.net_in_rate_kb = 0.0
        self.net_out_rate_kb = 0.0
        
        # 外部链路健康状态
        self.clob_rtt_ms = 0.0
        self.clob_status = "CONNECTING"
        self.clock_offset_ms = 0.0
        self.clock_status = "SYNCED"
        
        self._init_net_stats()
        self._start_probe_loop()

    def _start_probe_loop(self):
        t = threading.Thread(target=self._probe_external_links, daemon=True)
        t.start()

    def _probe_external_links(self):
        while True:
            # 1. 探测 Polymarket CLOB 接入点延迟
            try:
                ctx = ssl.create_default_context()
                host = "ws-subscriptions-clob.polymarket.com"
                t0 = time.perf_counter()
                s = socket.create_connection((host, 443), timeout=3)
                ss = ctx.wrap_socket(s, server_hostname=host)
                rtt = (time.perf_counter() - t0) * 1000
                ss.close()
                self.clob_rtt_ms = round(rtt, 1)
                self.clob_status = "HEALTHY" if rtt < 350 else "HIGH_LATENCY"
            except Exception:
                self.clob_rtt_ms = 0.0
                self.clob_status = "UNREACHABLE"

            # 2. 探测原子钟时钟偏移
            try:
                ctx = ssl.create_default_context()
                conn = http.client.HTTPSConnection("data-api.binance.vision", 443, context=ctx, timeout=3)
                t_send = time.time() * 1000
                conn.request("GET", "/api/v3/time", headers={"Connection": "close"})
                resp = conn.getresponse()
                body = json.loads(resp.read().decode())
                t_recv = time.time() * 1000
                server_ms = body["serverTime"]
                conn.close()
                offset = server_ms - ((t_send + t_recv) / 2.0)
                self.clock_offset_ms = round(offset, 2)
                self.clock_status = "SYNCED" if abs(offset) < 20 else "DRIFTING"
            except Exception:
                pass

            time.sleep(15)

    def _init_net_stats(self):
        in_b, out_b = self._get_raw_net_bytes()
        self._last_bytes_in = in_b
        self._last_bytes_out = out_b

    def _get_raw_net_bytes(self) -> tuple[int, int]:
        """获取系统接收/发送的总字节数"""
        if OS == "Linux" and os.path.exists("/proc/net/dev"):
            try:
                with open("/proc/net/dev", "r") as f:
                    lines = f.readlines()[2:]
                total_in = 0
                total_out = 0
                for line in lines:
                    parts = line.strip().split()
                    if len(parts) >= 10:
                        total_in += int(parts[1])
                        total_out += int(parts[9])
                return total_in, total_out
            except Exception:
                pass
        return 0, 0

    def get_cpu_mem(self) -> dict:
        """获取 CPU 与内存使用情况"""
        cpu_pct = 0.0
        mem_pct = 0.0
        mem_used_mb = 0
        mem_total_mb = 0

        if OS == "Linux":
            # 内存读取 /proc/meminfo
            if os.path.exists("/proc/meminfo"):
                try:
                    info = {}
                    with open("/proc/meminfo", "r") as f:
                        for line in f:
                            p = line.split(":")
                            if len(p) == 2:
                                info[p[0].strip()] = int(p[1].split()[0])
                    total_kb = info.get("MemTotal", 0)
                    avail_kb = info.get("MemAvailable", info.get("MemFree", 0))
                    used_kb = total_kb - avail_kb
                    mem_total_mb = round(total_kb / 1024, 1)
                    mem_used_mb = round(used_kb / 1024, 1)
                    if total_kb > 0:
                        mem_pct = round((used_kb / total_kb) * 100, 1)
                except Exception:
                    pass

            # CPU 负载读取 /proc/loadavg
            if os.path.exists("/proc/loadavg"):
                try:
                    with open("/proc/loadavg", "r") as f:
                        parts = f.read().split()
                    load1 = float(parts[0])
                    # 估算百分比（以 1 核为 100%）
                    cores = os.cpu_count() or 1
                    cpu_pct = round(min(100.0, (load1 / cores) * 100), 1)
                except Exception:
                    pass

        elif OS == "Windows":
            try:
                # 快速使用 powershell 简单查询
                out = subprocess.check_output(
                    ["powershell", "-Command", "Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize,FreePhysicalMemory | ConvertTo-Json"],
                    text=True, stderr=subprocess.DEVNULL, timeout=2
                )
                import json
                data = json.loads(out)
                total_kb = data.get("TotalVisibleMemorySize", 0)
                free_kb = data.get("FreePhysicalMemory", 0)
                used_kb = total_kb - free_kb
                mem_total_mb = round(total_kb / 1024, 1)
                mem_used_mb = round(used_kb / 1024, 1)
                mem_pct = round((used_kb / total_kb) * 100, 1) if total_kb else 0.0
            except Exception:
                pass

        return {
            "cpu_pct": cpu_pct,
            "mem_pct": mem_pct,
            "mem_used_mb": mem_used_mb,
            "mem_total_mb": mem_total_mb,
            "cpu_cores": os.cpu_count() or 1,
            "os": f"{platform.system()} {platform.release()}",
        }

    def update_network_rates(self):
        now = time.time()
        dt = now - self._last_net_check
        if dt >= 1.0:
            in_b, out_b = self._get_raw_net_bytes()
            if in_b > 0 and self._last_bytes_in > 0:
                self.net_in_rate_kb = round(max(0, in_b - self._last_bytes_in) / dt / 1024, 2)
                self.net_out_rate_kb = round(max(0, out_b - self._last_bytes_out) / dt / 1024, 2)
            self._last_bytes_in = in_b
            self._last_bytes_out = out_b
            self._last_net_check = now

    def get_stats(self) -> dict:
        self.update_network_rates()
        uptime_s = int(time.time() - self.start_time)
        days = uptime_s // 86400
        hours = (uptime_s % 86400) // 3600
        minutes = (uptime_s % 3600) // 60
        seconds = uptime_s % 60
        uptime_str = f"{days}天 {hours:02d}:{minutes:02d}:{seconds:02d}" if days > 0 else f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        cpu_mem = self.get_cpu_mem()
        return {
            "uptime_seconds": uptime_s,
            "uptime_str": uptime_str,
            "net_in_rate_kb": self.net_in_rate_kb,
            "net_out_rate_kb": self.net_out_rate_kb,
            "clob_rtt_ms": self.clob_rtt_ms,
            "clob_status": self.clob_status,
            "clock_offset_ms": self.clock_offset_ms,
            "clock_status": self.clock_status,
            **cpu_mem
        }
