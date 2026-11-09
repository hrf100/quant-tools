# btc_strategy/round_manager.py
"""
管理 Polymarket BTC 5分钟涨跌每一局的完整生命周期。

时间轴（以局开始时刻 T₀ 为基准）：
  T₀     : 本局开始，锁定开盘 TWAP 基准
  T₀+0s~: 实时结合宏观趋势 + RSI 计算前瞻方向
  T₀+20s~: 融合开盘后微观价格斜率，精细化方向与置信度
  T₀+300s: 本局到期，锁定收盘 TWAP，自动判定胜负并写入 SQLite
"""

import sqlite3
import threading
import time

from btc_strategy.config import (
    ROUND_DURATION_SEC, TWAP_WINDOW_SEC, DB_PATH
)
from btc_strategy.signal_engine import SignalEngine


def _now_ms() -> int:
    return int(time.time() * 1000)


def round_open_ts(ts: float = None) -> int:
    """返回当前局（或指定时刻所在局）的开始 Unix 秒。"""
    if ts is None:
        ts = time.time()
    return int(ts // ROUND_DURATION_SEC) * ROUND_DURATION_SEC


class RoundManager:

    def __init__(self, candle_engine, chainlink_poller):
        self._candles   = candle_engine
        self._chainlink = chainlink_poller
        self._signal    = SignalEngine()
        self._lock      = threading.Lock()
        self._thread    = None
        self._running   = False

        # 当前局状态
        self.current: dict = {}
        # 历史记录（内存缓存最近100局）
        self.history: list = []

        self._db_init()

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def get_current(self) -> dict:
        with self._lock:
            return dict(self.current)

    def get_history(self, limit: int = 50) -> list:
        with self._lock:
            return self.history[-limit:][::-1]  # 最新在前

    def get_stats(self) -> dict:
        with self._lock:
            hist = self.history
        total    = len([r for r in hist if r.get("actual_dir")])
        correct  = len([r for r in hist if r.get("is_correct") == 1 or r.get("is_correct") is True])
        skipped  = len([r for r in hist if r.get("predicted_dir") == "SKIP"])
        accuracy = round(correct / total * 100, 1) if total > 0 else 0.0
        
        streak = 0; streak_type = ""
        for r in reversed(hist):
            if r.get("actual_dir") is None:
                continue
            if r.get("predicted_dir") == "SKIP":
                continue
            is_c = (r.get("is_correct") == 1 or r.get("is_correct") is True)
            if streak == 0:
                streak_type = "W" if is_c else "L"
                streak = 1
            else:
                if (is_c and streak_type == "W") or (not is_c and streak_type == "L"):
                    streak += 1
                else:
                    break
        return {
            "total_rounds":    len(hist),
            "settled_rounds":  total,
            "correct":         correct,
            "skipped":         skipped,
            "accuracy_pct":    accuracy,
            "streak":          streak,
            "streak_type":     streak_type,
        }

    # ── 主循环 ────────────────────────────────────────────────────────────────

    def _run(self):
        while self._running:
            now       = time.time()
            open_ts   = round_open_ts(now)
            close_ts  = open_ts + ROUND_DURATION_SEC
            elapsed   = now - open_ts

            # 局切换检测
            with self._lock:
                if self.current.get("open_ts") != open_ts:
                    # 如果上一局还在，先结算上一局
                    prev_open = self.current.get("open_ts")
                    if prev_open and prev_open != open_ts and self.current.get("phase") != "SETTLED":
                        self._settle_round_internal(prev_open)
                    self._init_round(open_ts)

            # 确保开盘基准价已建立
            self._ensure_opening_price(open_ts)

            # 动态计算和更新信号
            self._update_live_signal(open_ts, elapsed)

            # 阶段判定
            if elapsed < 20:
                phase = "SAMPLING_MICRO"
            elif elapsed < ROUND_DURATION_SEC - TWAP_WINDOW_SEC:
                phase = "ACTIVE"
            else:
                phase = "SETTLEMENT_WINDOW"

            with self._lock:
                self.current["phase"] = phase
                self.current["time_remaining"] = max(0, int(close_ts - time.time()))
                self.current["elapsed"] = int(elapsed)
                self.current["chainlink_price"] = self._chainlink.price

            time.sleep(1)

    def _init_round(self, open_ts: int):
        """初始化新一局（已持有锁）。"""
        self.current = {
            "open_ts":        open_ts,
            "open_time_str":  time.strftime("%H:%M:%S", time.gmtime(open_ts)) + " UTC",
            "phase":          "INIT",
            "predicted_dir":  None,
            "confidence":     0.0,
            "score":          0.0,
            "breakdown":      {},
            "opening_price":  0.0,
            "chainlink_open": 0.0,
            "chainlink_price":0.0,
            "time_remaining": ROUND_DURATION_SEC,
            "elapsed":        0,
            "signal_computed":False,
        }
        self._db_insert_round(open_ts)

    def _ensure_opening_price(self, open_ts: int):
        """确保开盘基准价有效，绝不出现 0.0"""
        with self._lock:
            cur_op = self.current.get("opening_price", 0.0)
            if cur_op > 0:
                return

        twap = self._candles.get_twap(open_ts - TWAP_WINDOW_SEC, open_ts)
        if twap <= 0:
            twap = self._candles.last_price

        cl_p = self._chainlink.price

        if twap > 0:
            with self._lock:
                self.current["opening_price"] = round(twap, 2)
                self.current["chainlink_open"] = round(cl_p, 2)

    def _update_live_signal(self, open_ts: int, elapsed: float):
        """实时更新当前信号得分"""
        last_p = self._candles.last_price
        if last_p <= 0:
            return

        prices_since = self._candles.get_prices_since(open_ts)
        c1m = self._candles.get_candles("1m", 60)
        c5m = self._candles.get_candles("5m", 50)
        c1h = self._candles.get_candles("1h", 48)

        result = self._signal.compute(
            c1m, c5m, c1h,
            last_p,
            elapsed,
            prices_since,
        )

        with self._lock:
            self.current.update({
                "signal_computed": True,
                "predicted_dir":   result["direction"],
                "confidence":      result["confidence"],
                "score":           result["score"],
                "breakdown":       result["breakdown"],
            })

    def _settle_round_internal(self, open_ts: int):
        """结算指定的一局"""
        close_ts = open_ts + ROUND_DURATION_SEC
        closing_price = self._candles.get_twap(close_ts - TWAP_WINDOW_SEC, close_ts)
        if closing_price <= 0:
            closing_price = self._candles.last_price

        opening_price = self.current.get("opening_price", 0.0) or closing_price
        actual_dir = "UP" if closing_price >= opening_price else "DOWN"
        predicted = self.current.get("predicted_dir")
        
        is_correct = None
        if predicted and predicted != "SKIP":
            is_correct = (predicted == actual_dir)

        cl_close = self._chainlink.price

        self._db_settle(open_ts, closing_price, cl_close, actual_dir, is_correct)

        record = {
            **self.current,
            "closing_price": round(closing_price, 2),
            "chainlink_close": round(cl_close, 2),
            "actual_dir": actual_dir,
            "is_correct": is_correct,
            "phase": "SETTLED"
        }
        self.history.append(record)
        if len(self.history) > 200:
            self.history = self.history[-200:]

    # ── 数据库持久化 ──────────────────────────────────────────────────────────

    def _db_init(self):
        con = sqlite3.connect(DB_PATH)
        con.execute("""
            CREATE TABLE IF NOT EXISTS rounds (
                open_ts         INTEGER PRIMARY KEY,
                predicted_dir   TEXT,
                confidence      REAL,
                score           REAL,
                score_macro     REAL,
                score_rsi       REAL,
                score_bb        REAL,
                score_momentum  REAL,
                score_micro     REAL,
                opening_price   REAL,
                closing_price   REAL,
                chainlink_open  REAL,
                chainlink_close REAL,
                actual_dir      TEXT,
                is_correct      INTEGER,
                created_at      INTEGER
            )
        """)
        con.commit()
        rows = con.execute(
            "SELECT * FROM rounds WHERE actual_dir IS NOT NULL ORDER BY open_ts DESC LIMIT 200"
        ).fetchall()
        cols = [d[0] for d in con.execute("PRAGMA table_info(rounds)").fetchall()]
        con.close()
        with self._lock:
            self.history = [dict(zip(cols, r)) for r in reversed(rows)]

    def _db_insert_round(self, open_ts: int):
        try:
            con = sqlite3.connect(DB_PATH)
            con.execute(
                "INSERT OR IGNORE INTO rounds (open_ts, created_at) VALUES (?, ?)",
                (open_ts, _now_ms()))
            con.commit(); con.close()
        except Exception: pass

    def _db_settle(self, open_ts: int, closing: float, cl_close: float,
                   actual: str, is_correct):
        try:
            con = sqlite3.connect(DB_PATH)
            con.execute("""
                UPDATE rounds SET
                    closing_price=?, chainlink_close=?,
                    actual_dir=?, is_correct=?
                WHERE open_ts=?
            """, (closing, cl_close, actual,
                  int(is_correct) if is_correct is not None else None,
                  open_ts))
            con.commit(); con.close()
        except Exception: pass
