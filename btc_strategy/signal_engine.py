# btc_strategy/signal_engine.py
"""
三层信号融合评分系统，只使用开场前已经知道的数据：
  Layer 1: 宏观趋势  (1h EMA-21)        权重 ±25
  Layer 2: 结构信号  (已收盘 5m RSI + BB + 上一根 5m 方向)  权重 ±35
  Layer 3: 微观斜率  (开场前价格斜率)    权重 ±25
  Total: ±85 → 归一化到 ±100
  |score| > SIGNAL_THRESHOLD → UP/DOWN，否则 SKIP
"""

import math
from btc_strategy.config import (
    PREOPEN_SLOPE_MIN_SPAN,
    PREOPEN_SLOPE_MIN_TICKS,
    SIGNAL_THRESHOLD,
)


# ── 数学工具 ──────────────────────────────────────────────────────────────────

def _ema(values: list[float], n: int) -> float:
    """指数移动平均（Wilder 平滑）。"""
    if len(values) < n:
        return values[-1] if values else 0.0
    alpha = 2.0 / (n + 1)
    result = sum(values[:n]) / n  # 用简单均值作为种子
    for v in values[n:]:
        result = alpha * v + (1 - alpha) * result
    return result


def _rsi(closes: list[float], n: int = 7) -> float:
    """Wilder RSI，返回 0~100。"""
    if len(closes) < n + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_g  = sum(gains[:n]) / n
    avg_l  = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1 + rs)


def _bollinger(closes: list[float], n: int = 20, k: float = 2.0):
    """返回 (upper, mid, lower)，closes 长度须 >= n。"""
    window = closes[-n:]
    ma  = sum(window) / n
    std = math.sqrt(sum((c - ma) ** 2 for c in window) / n)
    return ma + k * std, ma, ma - k * std


def _linear_slope_per_sec(points: list[tuple[float, float]]) -> float:
    """价格对时间的最小二乘斜率，单位 USD/秒。"""
    n = len(points)
    if n < 2:
        return 0.0
    t0 = points[0][0]
    xs = [t - t0 for t, _ in points]
    ys = [p for _, p in points]
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denom = sum((x - mx) ** 2 for x in xs)
    return num / denom if denom else 0.0


# ── 核心信号引擎 ──────────────────────────────────────────────────────────────

class SignalEngine:

    def compute(
        self,
        candles_5m:  list[dict],
        candles_1h:  list[dict],
        current_price: float,
        prices_before_open: list[tuple[float, float]],  # (ts, price)，全部早于开场
    ) -> dict:
        """
        返回:
          direction    : 'UP' | 'DOWN' | 'SKIP'
          score        : float，-100 ~ +100（正值偏UP）
          confidence   : float，0~100
          breakdown    : dict，各层得分明细
        调用方必须保证 K 线收盘价和价格序列都不含本局开场后的成交。
        """
        breakdown = {}
        raw_score = 0.0

        # ── Layer 1: 宏观趋势（±25）────────────────────────────────────────────
        macro = self._layer_macro(candles_1h, current_price)
        breakdown["macro"]     = macro
        breakdown["macro_lbl"] = "1h EMA-21"
        raw_score += macro

        # ── Layer 2a: RSI（±15）────────────────────────────────────────────────
        rsi_score, rsi_val = self._layer_rsi(candles_5m)
        breakdown["rsi"]     = rsi_score
        breakdown["rsi_val"] = round(rsi_val, 1)
        raw_score += rsi_score

        # ── Layer 2b: Bollinger Band 位置（±10）────────────────────────────────
        bb_score, bb_pct = self._layer_bb(candles_5m, current_price)
        breakdown["bb"]     = bb_score
        breakdown["bb_pct"] = round(bb_pct * 100, 1)  # % 距下轨
        raw_score += bb_score

        # ── Layer 2c: 5m K线动能（±10）─────────────────────────────────────────
        mom_score = self._layer_momentum(candles_5m)
        breakdown["momentum"] = mom_score
        raw_score += mom_score

        # ── Layer 3: 开场前微观斜率（±25）─────────────────────────────────────
        micro, slope_usd_s = self._layer_micro(prices_before_open)
        breakdown["micro"]       = round(micro, 1)
        breakdown["slope_usd_s"] = round(slope_usd_s, 3)
        raw_score += micro

        # ── 归一化 ──────────────────────────────────────────────────────────────
        MAX_RAW = 25 + 15 + 10 + 10 + 25  # = 85
        score   = raw_score / MAX_RAW * 100.0
        score   = max(-100.0, min(100.0, score))

        if score > SIGNAL_THRESHOLD:
            direction = "UP"
        elif score < -SIGNAL_THRESHOLD:
            direction = "DOWN"
        else:
            direction = "SKIP"

        confidence = min(100.0, abs(score))

        return {
            "direction":  direction,
            "score":      round(score, 1),
            "confidence": round(confidence, 1),
            "breakdown":  breakdown,
        }

    # ── 各层计算 ──────────────────────────────────────────────────────────────

    def _layer_macro(self, candles_1h: list[dict], price: float) -> float:
        closes = [c["close"] for c in candles_1h]
        if len(closes) < 22:
            return 0.0
        ema21 = _ema(closes, 21)
        return 25.0 if price > ema21 else -25.0

    def _layer_rsi(self, candles_5m: list[dict]) -> tuple[float, float]:
        closes = [c["close"] for c in candles_5m]
        if len(closes) < 10:
            return 0.0, 50.0
        rsi = _rsi(closes, 7)
        if rsi > 75:   s = -15.0
        elif rsi > 65: s = -8.0
        elif rsi < 25: s = 15.0
        elif rsi < 35: s = 8.0
        elif rsi > 52: s = 4.0
        elif rsi < 48: s = -4.0
        else:          s = 0.0
        return s, rsi

    def _layer_bb(self, candles_5m: list[dict], price: float) -> tuple[float, float]:
        closes = [c["close"] for c in candles_5m]
        if len(closes) < 20:
            return 0.0, 0.5
        upper, mid, lower = _bollinger(closes, 20, 2.0)
        band = upper - lower
        if band <= 0:
            return 0.0, 0.5
        pct = (price - lower) / band  # 0=下轨, 1=上轨
        if pct > 0.97:   s = -10.0
        elif pct > 0.80: s = -5.0
        elif pct < 0.03: s = 10.0
        elif pct < 0.20: s = 5.0
        else:            s = 0.0
        return s, pct

    def _layer_momentum(self, candles_5m: list[dict]) -> float:
        if len(candles_5m) < 2:
            return 0.0
        last    = candles_5m[-1]
        body    = last["close"] - last["open"]
        avg_vol = sum(c["volume"] for c in candles_5m[-10:]) / min(10, len(candles_5m))
        vol_r   = last["volume"] / avg_vol if avg_vol > 0 else 1.0
        strong  = vol_r > 1.15
        if body > 0:   return 10.0 if strong else 5.0
        elif body < 0: return -10.0 if strong else -5.0
        return 0.0

    def _layer_micro(
        self,
        prices_before_open: list[tuple[float, float]],
    ) -> tuple[float, float]:
        if len(prices_before_open) < PREOPEN_SLOPE_MIN_TICKS:
            return 0.0, 0.0
        span = prices_before_open[-1][0] - prices_before_open[0][0]
        if span < PREOPEN_SLOPE_MIN_SPAN:
            return 0.0, 0.0
        slope_usd_s = _linear_slope_per_sec(prices_before_open)
        # 0.5 USD/s → ±25 分
        micro = max(-25.0, min(25.0, slope_usd_s * 50.0))
        return micro, slope_usd_s
