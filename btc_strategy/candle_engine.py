# btc_strategy/candle_engine.py
"""
从逐笔成交（aggTrade）构建 1m / 5m / 1h OHLCV 蜡烛。
线程安全，纯标准库，零依赖。
"""

import threading
import time
from collections import deque
from btc_strategy.config import CANDLE_MAX_COUNT


class Candle:
    __slots__ = ("open_time", "open", "high", "low", "close", "volume", "tick_count")

    def __init__(self, open_time: float, price: float):
        self.open_time  = open_time
        self.open       = price
        self.high       = price
        self.low        = price
        self.close      = price
        self.volume     = 0.0
        self.tick_count = 0

    def update(self, price: float, volume: float):
        if price > self.high: self.high = price
        if price < self.low:  self.low  = price
        self.close       = price
        self.volume     += volume
        self.tick_count += 1

    def to_dict(self) -> dict:
        return {
            "open_time":  self.open_time,
            "open":       round(self.open,  2),
            "high":       round(self.high,  2),
            "low":        round(self.low,   2),
            "close":      round(self.close, 2),
            "volume":     round(self.volume, 4),
            "tick_count": self.tick_count,
        }


class CandleEngine:
    """
    聚合逐笔成交为 OHLCV 蜡烛。
    支持 '1m' / '5m' / '1h' 三个周期。
    维护一个时间戳+价格的滚动缓冲，用于 TWAP 计算。
    """

    TF_SECONDS = {"1m": 60, "5m": 300, "1h": 3600}

    def __init__(self):
        self._lock = threading.Lock()
        self._closed: dict[str, deque] = {
            tf: deque(maxlen=CANDLE_MAX_COUNT) for tf in self.TF_SECONDS
        }
        self._current: dict[str, Candle | None] = {tf: None for tf in self.TF_SECONDS}
        # 滚动价格缓冲：(timestamp, price)，保留最近 10 分钟
        self._price_buf: deque[tuple[float, float]] = deque(maxlen=12000)
        self.last_price: float = 0.0
        self.tick_count: int   = 0

    # ── 公开接口 ────────────────────────────────────────────────────────────────

    def on_tick(self, price: float, volume: float, ts: float | None = None):
        """注入一笔成交。"""
        if ts is None:
            ts = time.time()
        with self._lock:
            self.last_price  = price
            self.tick_count += 1
            self._price_buf.append((ts, price))
            self._update_all_candles(price, volume, ts)

    def get_candles(self, tf: str, count: int = 60) -> list[dict]:
        """返回已收盘蜡烛列表（最新在最后），最多 count 根，包含当前未收盘蜡烛。"""
        with self._lock:
            closed  = list(self._closed[tf])[-count:]
            result  = [c.to_dict() for c in closed]
            curr    = self._current[tf]
            if curr and curr.tick_count > 0:
                result.append(curr.to_dict())
            return result

    def get_twap(self, start_ts: float, end_ts: float) -> float:
        """计算指定时间区间内的时间加权均价（简单均价近似）。"""
        with self._lock:
            prices = [p for t, p in self._price_buf if start_ts <= t <= end_ts]
        return sum(prices) / len(prices) if prices else 0.0

    def get_prices_since(self, since_ts: float) -> list[tuple[float, float]]:
        """返回 since_ts 之后的 (timestamp, price) 列表（用于微斜率计算）。"""
        with self._lock:
            return [(t, p) for t, p in self._price_buf if t >= since_ts]

    # ── 内部方法 ─────────────────────────────────────────────────────────────────

    def _update_all_candles(self, price: float, volume: float, ts: float):
        for tf, interval_s in self.TF_SECONDS.items():
            open_time = int(ts // interval_s) * interval_s
            curr      = self._current[tf]
            if curr is None:
                self._current[tf] = Candle(open_time, price)
            elif curr.open_time != open_time:
                self._closed[tf].append(curr)
                self._current[tf] = Candle(open_time, price)
            else:
                curr.update(price, volume)
