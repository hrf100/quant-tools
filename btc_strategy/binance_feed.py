# btc_strategy/binance_feed.py
"""
Binance BTC/USDT aggTrade WebSocket 持久连接。
- 自动重连（指数退避）
- 纯 Python 标准库实现 RFC 6455
- 实时统计：消息速率、字节速率、重连次数
"""

import base64
import json
import os
import socket
import ssl
import struct
import threading
import time

from btc_strategy.config import BINANCE_WS_HOST, BINANCE_WS_PORT, BINANCE_WS_PATH


def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def _read_frame(sock) -> tuple[int, bytes]:
    """读取一个 WebSocket 帧，返回 (opcode, payload)。"""
    hdr    = _recv_exact(sock, 2)
    opcode = hdr[0] & 0x0F
    masked = (hdr[1] >> 7) & 1
    plen   = hdr[1] & 0x7F
    if plen == 126:
        plen = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif plen == 127:
        plen = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask_key = _recv_exact(sock, 4) if masked else None
    payload  = _recv_exact(sock, plen)
    if mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _send_frame(sock, opcode: int, payload: bytes = b""):
    """发送一个带掩码的 WebSocket 帧（客户端规范要求）。"""
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header += bytearray([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytearray([0x80 | 127]) + struct.pack(">Q", n)
    header += mask
    sock.sendall(bytes(header) + masked)


class BinanceFeed:
    """
    持久 Binance aggTrade WebSocket。
    通过 on_tick(price, volume, ts) 回调传递数据。
    """

    def __init__(self, on_tick_cb):
        self._on_tick     = on_tick_cb
        self._running     = False
        self._thread      = None
        self._sock        = None
        # 统计
        self.connected        = False
        self.connect_time     = 0.0
        self.reconnect_count  = 0
        self.msg_total        = 0
        self.bytes_total      = 0
        self.ping_rtt_ms      = 0.0
        self._last_ping_ts    = 0.0
        self.endpoint         = f"{BINANCE_WS_HOST}:{BINANCE_WS_PORT}"
        self._stat_lock       = threading.Lock()
        # 速率计算（1秒窗口）
        self._rate_msgs_buf   = []   # (timestamp,)
        self._rate_bytes_buf  = []   # (timestamp, bytes)
        self.msg_rate         = 0.0
        self.byte_rate        = 0.0

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._sock:
            try: _send_frame(self._sock, 0x08)  # Close frame
            except: pass

    def get_stats(self) -> dict:
        self._update_rates()
        uptime = time.time() - self.connect_time if self.connected else 0
        return {
            "connected":       self.connected,
            "endpoint":        self.endpoint,
            "reconnect_count": self.reconnect_count,
            "msg_total":       self.msg_total,
            "bytes_total":     self.bytes_total,
            "msg_rate":        round(self.msg_rate, 1),
            "byte_rate_kb":    round(self.byte_rate / 1024, 2),
            "ping_rtt_ms":     self.ping_rtt_ms,
            "uptime_s":        round(uptime, 0),
        }

    # ── 内部 ─────────────────────────────────────────────────────────────────

    def _run_loop(self):
        backoff = 1.0
        while self._running:
            try:
                self._connect_and_receive()
                backoff = 1.0
            except Exception as e:
                self.connected = False
                if self._running:
                    with self._stat_lock:
                        self.reconnect_count += 1
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    def _connect_and_receive(self):
        ctx  = ssl.create_default_context()
        ip   = socket.gethostbyname(BINANCE_WS_HOST)
        raw  = socket.create_connection((ip, BINANCE_WS_PORT), timeout=10)
        sock = ctx.wrap_socket(raw, server_hostname=BINANCE_WS_HOST)
        sock.settimeout(30)

        # HTTP Upgrade 握手
        key = base64.b64encode(os.urandom(16)).decode()
        hs  = (
            f"GET {BINANCE_WS_PATH} HTTP/1.1\r\n"
            f"Host: {BINANCE_WS_HOST}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(hs.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += sock.recv(1024)
        if b"101" not in resp:
            raise ConnectionError("WebSocket upgrade failed")

        self._sock        = sock
        self.connected    = True
        self.connect_time = time.time()

        # 心跳线程：每 20s 发 Ping
        ping_t = threading.Thread(target=self._ping_loop, args=(sock,), daemon=True)
        ping_t.start()

        # 接收循环
        frag_buf = b""
        while self._running:
            opcode, payload = _read_frame(sock)
            if opcode == 0x01 or opcode == 0x02:  # Text / Binary
                self._handle_message(payload)
            elif opcode == 0x00:                   # Continuation
                frag_buf += payload
            elif opcode == 0x09:                   # Ping
                _send_frame(sock, 0x0A, payload)   # Pong
            elif opcode == 0x0A:                   # Pong
                if self._last_ping_ts > 0:
                    self.ping_rtt_ms = round((time.perf_counter() - self._last_ping_ts) * 1000, 1)
            elif opcode == 0x08:                   # Close
                break

    def _handle_message(self, payload: bytes):
        with self._stat_lock:
            self.msg_total   += 1
            self.bytes_total += len(payload)
            now = time.time()
            self._rate_msgs_buf.append(now)
            self._rate_bytes_buf.append((now, len(payload)))
        try:
            data   = json.loads(payload.decode("utf-8"))
            price  = float(data["p"])
            volume = float(data["q"])
            ts_ms  = int(data["T"])
            self._on_tick(price, volume, ts_ms / 1000.0)
        except Exception:
            pass

    def _ping_loop(self, sock):
        while self._running and self.connected:
            try:
                self._last_ping_ts = time.perf_counter()
                _send_frame(sock, 0x09)
            except Exception:
                break
            time.sleep(10)

    def _update_rates(self):
        now    = time.time()
        cutoff = now - 5.0  # 5 秒窗口平均
        with self._stat_lock:
            self._rate_msgs_buf  = [t for t in self._rate_msgs_buf  if t > cutoff]
            self._rate_bytes_buf = [(t, b) for t, b in self._rate_bytes_buf if t > cutoff]
            self.msg_rate  = len(self._rate_msgs_buf) / 5.0
            self.byte_rate = sum(b for _, b in self._rate_bytes_buf) / 5.0
