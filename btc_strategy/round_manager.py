# btc_strategy/round_manager.py
"""
管理 Polymarket BTC 5分钟涨跌每一局的完整生命周期。

时间轴（以局开始时刻 T₀ 为基准）：
  T₀-60s : 开始采样开盘 TWAP 基准
  T₀     : 本局正式开始，开盘 TWAP 锁定
  T₀+30s : 计算信号，作出方向预测
  T₀+240s: 开始采样收盘 TWAP
  T₀+300s: 本局结束，收盘 TWAP 锁定，判断实际方向，写入数据库
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
        correct  = len([r for r in hist if r.get("is_correct")])
        skipped  = len([r for r in hist if r.get("predicted_dir") == "SKIP"])
        accuracy = round(correct / total * 100, 1) if total > 0 else 0.0
        # 连胜/连败
        streak = 0; streak_type = ""
        for r in reversed(hist):
            if r.get("actual_dir") is None:
                continue
            if r.get("predicted_dir") == "SKIP":
                break
            if r.get("is_correct"):
                if streak_type == "W": streak += 1
                else: streak = 1; streak_type = "W"
            else:
                if streak_type == "L": streak += 1
                else: streak = 1; streak_type = "L"
            break
        # 继续统计连续
        for r in reversed(hist[:-1]):
            if r.get("actual_dir") is None or r.get("predicted_dir") == "SKIP":
                break
            if (r.get("is_correct") and streak_type == "W") or \
               (not r.get("is_correct") and streak_type == "L"):
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

            # 初始化当前局（如果还没）
            with self._lock:
                if self.current.get("open_ts") != open_ts:
                    self._init_round(open_ts)

            # 状态机
            if elapsed < 0:
                pass  # 不应发生
            elif elapsed < TWAP_WINDOW_SEC:
                # 阶段1：采样开盘 TWAP（T₀-60s ~ T₀，此时正好在上一局最后60秒）
                self._update_current("phase", "SAMPLING_OPEN")
            elif elapsed < 30:
                # 阶段2：等待微斜率数据（T₀ ~ T₀+30s）
                self._update_current("phase", "AWAITING_MICRO")
            elif elapsed < ROUND_DURATION_SEC - TWAP_WINDOW_SEC:
                # 阶段3：信号已计算（或计算中）
                self._ensure_signal_computed(open_ts)
                self._update_current("phase", "ACTIVE")
            elif elapsed < ROUND_DURATION_SEC:
                # 阶段4：采样收盘 TWAP
                self._ensure_signal_computed(open_ts)
                self._update_current("phase", "SAMPLING_CLOSE")
            else:
                # 阶段5：本局结束，结算
                self._settle_round(open_ts)

            # 更新实时倒计时
            self._update_current("time_remaining", max(0, int(close_ts - time.time())))
            self._update_current("elapsed", int(elapsed))
            self._update_current("chainlink_price", self._chainlink.price)

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
        # 预写数据库行
        self._db_insert_round(open_ts)

    def _update_current(self, key: str, value):
        with self._lock:
            if key in self.current:
                self.current[key] = value

    def _ensure_signal_computed(self, open_ts: int):
        with self._lock:
            if self.current.get("signal_computed"):
                return

        # 计算开盘 TWAP（上一局最后60秒 ~ 本局开始）
        opening_price = self._candles.get_twap(open_ts - TWAP_WINDOW_SEC, open_ts)
        if opening_price == 0.0:
            opening_price = self._candles.last_price

        # 获取微斜率数据（开盘后价格）
        prices_since = self._candles.get_prices_since(open_ts)

        # 信号计算
        c1m = self._candles.get_candles("1m", 60)
        c5m = self._candles.get_candles("5m", 50)
        c1h = self._candles.get_candles("1h", 48)
        elapsed = time.time() - open_ts

        result = self._signal.compute(
            c1m, c5m, c1h,
            self._candles.last_price,
            elapsed,
            prices_since,
        )

        cl_price = self._chainlink.price

        with self._lock:
            self.current.update({
                "signal_computed": True,
                "predicted_dir":   result["direction"],
                "confidence":      result["confidence"],
                "score":           result["score"],
                "breakdown":       result["breakdown"],
                "opening_price":   round(opening_price, 2),
                "chainlink_open":  round(cl_price, 2),
            })

        # 写入数据库
        self._db_update_signal(open_ts, result, opening_price, cl_price)

    def _settle_round(self, open_ts: int):
        """计算结果并写入数据库，然后归档到历史。"""
        with self._lock:
            if self.current.get("phase") == "SETTLED":
                return
            self.current["phase"] = "SETTLED"
            round_snapshot = dict(self.current)

        close_ts     = open_ts + ROUND_DURATION_SEC
        closing_price = self._candles.get_twap(close_ts - TWAP_WINDOW_SEC, close_ts)
        if closing_price == 0.0:
            closing_price = self._candles.last_price

        opening_price = round_snapshot.get("opening_price", 0.0) or self._candles.last_price
        actual_dir    = "UP" if closing_price >= opening_price else "DOWN"
        predicted     = round_snapshot.get("predicted_dir")
        is_correct    = None
        if predicted and predicted != "SKIP":
            is_correct = (predicted == actual_dir)

        cl_close = self._chainlink.price

        # 写数据库
        self._db_settle(open_ts, closing_price, cl_close, actual_dir, is_correct)

        # 归档历史
        record = {**round_snapshot,
                  "closing_price": round(closing_price, 2),
                  "chainlink_close": round(cl_close, 2),
                  "actual_dir":    actual_dir,
                  "is_correct":    is_correct}
        with self._lock:
            self.history.append(record)
            if len(self.history) > 200:
                self.history = self.history[-200:]

    # ── 数据库 ───────────────────────────────────────────────────────────────

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
        # 加载最近 200 局到内存
        rows = con.execute(
            "SELECT * FROM rounds ORDER BY open_ts DESC LIMIT 200"
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

    def _db_update_signal(self, open_ts: int, result: dict, opening_price: float, cl_open: float):
        try:
            bd  = result.get("breakdown", {})
            con = sqlite3.connect(DB_PATH)
            con.execute("""
                UPDATE rounds SET
                    predicted_dir=?, confidence=?, score=?,
                    score_macro=?, score_rsi=?, score_bb=?,
                    score_momentum=?, score_micro=?,
                    opening_price=?, chainlink_open=?
                WHERE open_ts=?
            """, (result["direction"], result["confidence"], result["score"],
                  bd.get("macro",0), bd.get("rsi",0), bd.get("bb",0),
                  bd.get("momentum",0), bd.get("micro_slope",0),
                  opening_price, cl_open, open_ts))
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
