"""看板请求流量与行情下行流量。"""

import threading
import time
from collections import deque


class TrafficStats:

    def __init__(self):
        self._lock = threading.Lock()
        self.started = time.time()
        self.http_requests = 0
        self.http_bytes_in = 0
        self.http_bytes_out = 0
        self._events = deque()  # (ts, kind, nbytes)
        self._binance_marks = deque()  # (ts, cumulative bytes)

    def record_http(self, bytes_in: int, bytes_out: int):
        now = time.time()
        with self._lock:
            self.http_requests += 1
            self.http_bytes_in += max(0, int(bytes_in))
            self.http_bytes_out += max(0, int(bytes_out))
            self._events.append((now, "req", 1))
            if bytes_in:
                self._events.append((now, "in", int(bytes_in)))
            if bytes_out:
                self._events.append((now, "out", int(bytes_out)))
            self._trim(now)

    def note_binance(self, cumulative_bytes: int):
        now = time.time()
        with self._lock:
            self._binance_marks.append((now, int(cumulative_bytes)))
            self._trim(now)

    def snapshot(self, binance_bytes_total: int = 0) -> dict:
        now = time.time()
        with self._lock:
            self.note_binance_unlocked(now, binance_bytes_total)
            self._trim(now)
            http_in_60, http_out_60, _ = self._between(now - 60, now)
            http_n_60 = self._count(now - 60, now, "req")
            binance_60 = self._binance_delta(now, 60)
            minutes = []
            for i in range(14, -1, -1):
                end = now - i * 60
                start = end - 60
                inn, out, _ = self._between(start, end)
                minutes.append({
                    "label": time.strftime("%H:%M", time.localtime(start)),
                    "http_kb": round((inn + out) / 1024, 2),
                    "binance_kb": round(self._binance_between(start, end) / 1024, 2),
                })
            uptime = max(1.0, now - self.started)
            return {
                "uptime_sec": int(uptime),
                "http_requests": self.http_requests,
                "http_bytes_in": self.http_bytes_in,
                "http_bytes_out": self.http_bytes_out,
                "http_in_kb": round(self.http_bytes_in / 1024, 2),
                "http_out_kb": round(self.http_bytes_out / 1024, 2),
                "http_req_per_min": round(http_n_60, 1),
                "http_in_kbps": round(http_in_60 / 1024 / 60, 3),
                "http_out_kbps": round(http_out_60 / 1024 / 60, 3),
                "binance_bytes_total": int(binance_bytes_total),
                "binance_mb": round(int(binance_bytes_total) / (1024 * 1024), 3),
                "binance_kbps": round(binance_60 / 1024 / 60, 3),
                "minutes": minutes,
            }

    def note_binance_unlocked(self, now: float, cumulative_bytes: int):
        if not self._binance_marks or now - self._binance_marks[-1][0] >= 1:
            self._binance_marks.append((now, int(cumulative_bytes)))

    def _trim(self, now: float):
        cutoff = now - 20 * 60
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        while self._binance_marks and self._binance_marks[0][0] < cutoff:
            self._binance_marks.popleft()

    def _count(self, start: float, end: float, kind: str) -> int:
        return sum(1 for ts, k, _ in self._events if start <= ts < end and k == kind)

    def _between(self, start: float, end: float):
        inn = out = n = 0
        for ts, kind, nbytes in self._events:
            if start <= ts < end:
                n += 1
                if kind == "in":
                    inn += nbytes
                else:
                    out += nbytes
        return inn, out, n

    def _binance_delta(self, now: float, seconds: float) -> int:
        return self._binance_between(now - seconds, now)

    def _binance_between(self, start: float, end: float) -> int:
        marks = [(ts, b) for ts, b in self._binance_marks if ts <= end]
        if len(marks) < 2:
            return 0
        before = [b for ts, b in marks if ts <= start]
        base = before[-1] if before else marks[0][1]
        return max(0, marks[-1][1] - base)
