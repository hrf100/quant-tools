# btc_strategy/round_manager.py
"""
管理 Polymarket BTC 5分钟涨跌每一局的完整生命周期。

时间轴（以局开始时刻 T₀ 为基准）：
  T₀ 之前 : 用开场前的 K 线与价格给出本局方向，并在 T₀ 锁定
  T₀      : 本局开始。方向不再改写。开盘基准只用 T₀ 之前的成交
  T₀~T₁   : 同时预告下一场，预告随开场前数据更新，直到下一场 T₀
  到期    : 锁定收盘均价，和开场前预测比较后写入 SQLite
"""

import sqlite3
import threading
import time

from btc_strategy.config import (
    DB_PATH, PREOPEN_SLOPE_SEC, ROUND_DURATION_SEC, TWAP_WINDOW_SEC,
)
from btc_strategy.candle_engine import CandleEngine
from btc_strategy.signal_engine import SignalEngine


def _now_ms() -> int:
    return int(time.time() * 1000)


def round_open_ts(ts: float = None) -> int:
    """返回当前局（或指定时刻所在局）的开始 Unix 秒。"""
    if ts is None:
        ts = time.time()
    return int(ts // ROUND_DURATION_SEC) * ROUND_DURATION_SEC


class RoundManager:

    def __init__(self, candle_engine, chainlink_poller, db_path: str = None):
        self._candles   = candle_engine
        self._chainlink = chainlink_poller
        self._signal    = SignalEngine()
        self._db_path   = db_path or DB_PATH
        self._lock      = threading.Lock()
        self._thread    = None
        self._running   = False

        # 当前局状态（方向在开场时锁定）
        self.current: dict = {}
        # 下一场预告（开场前可持续更新）
        self.upcoming: dict = {}
        # 历史记录（内存缓存最近 200 局）
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

    def get_upcoming(self) -> dict:
        with self._lock:
            return dict(self.upcoming)

    def get_history(self, limit: int = 50) -> list:
        with self._lock:
            return self.history[-limit:][::-1]  # 最新在前

    def get_stats(self) -> dict:
        with self._lock:
            hist = list(self.history)
        settled  = [r for r in hist if r.get("actual_dir")]
        called   = [r for r in settled if r.get("predicted_dir") in ("UP", "DOWN")]
        correct  = len([r for r in called if r.get("is_correct") == 1 or r.get("is_correct") is True])
        skipped  = len([r for r in settled if r.get("predicted_dir") == "SKIP"])
        no_call  = len([r for r in settled if not r.get("predicted_dir")])
        total    = len(settled)
        accuracy = round(correct / len(called) * 100, 1) if called else 0.0
        
        streak = 0; streak_type = ""
        for r in reversed(hist):
            if r.get("actual_dir") is None:
                continue
            if r.get("predicted_dir") not in ("UP", "DOWN"):
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
            "called_rounds":   len(called),
            "correct":         correct,
            "skipped":         skipped,
            "no_call":         no_call,
            "accuracy_pct":    accuracy,
            "streak":          streak,
            "streak_type":     streak_type,
        }

    def clear_history(self) -> None:
        """删除已结算场次。当前场次的预测和库记录保留。"""
        with self._lock:
            keep = self.current.get("open_ts")
            self.history = [
                row for row in self.history
                if keep and row.get("open_ts") == keep and not row.get("actual_dir")
            ]
        con = sqlite3.connect(self._db_path)
        try:
            if keep:
                con.execute("DELETE FROM rounds WHERE open_ts != ?", (keep,))
            else:
                con.execute("DELETE FROM rounds")
            con.commit()
        finally:
            con.close()

    # ── 主循环 ────────────────────────────────────────────────────────────────

    def _run(self):
        while self._running:
            self._tick(time.time())
            time.sleep(1)

    def _tick(self, now: float):
        """推进到指定时刻。开场瞬间锁定本局预测，之后只更新下一场预告。"""
        open_ts  = round_open_ts(now)
        close_ts = open_ts + ROUND_DURATION_SEC
        elapsed  = now - open_ts
        switched = False

        with self._lock:
            if self.current.get("open_ts") != open_ts:
                prev_open = self.current.get("open_ts")
                if prev_open and prev_open != open_ts and self.current.get("phase") != "SETTLED":
                    self._settle_round_internal(prev_open)
                self._init_round(open_ts)
                switched = True

        if switched:
            self._lock_preopen_signal(open_ts)

        self._ensure_opening_price(open_ts)
        self._refresh_upcoming(close_ts, now)

        with self._lock:
            has_call = self.current.get("predicted_dir") in ("UP", "DOWN", "SKIP")
            if elapsed >= ROUND_DURATION_SEC - TWAP_WINDOW_SEC and has_call:
                phase = "SETTLEMENT_WINDOW"
            elif has_call:
                phase = "LOCKED"
            else:
                phase = "NO_CALL"
            self.current["phase"] = phase
            self.current["time_remaining"] = max(0, int(close_ts - now))
            self.current["elapsed"] = int(elapsed)
            self.current["chainlink_price"] = self._chainlink.price

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
            "signal_computed": False,
            "signal_locked":  False,
        }
        self._db_insert_round(open_ts)

    def _lock_preopen_signal(self, open_ts: int):
        """把开场前的预测写入本局。本局进行中不会再调用。"""
        restored = self._db_load_prediction(open_ts)
        if restored:
            with self._lock:
                self.current.update(restored)
            return

        result = self._signal_asof(open_ts)
        if result is None:
            return

        with self._lock:
            self.current.update({
                "signal_locked":   True,
                "signal_computed": True,
                "predicted_dir":   result["direction"],
                "confidence":      result["confidence"],
                "score":           result["score"],
                "breakdown":       result["breakdown"],
            })
            snap = dict(self.current)
        self._db_save_prediction(snap)

    def _refresh_upcoming(self, next_open: int, now: float):
        """下一场预告。next_open 仍在未来，因此用到的全是开场前数据。"""
        result = self._signal_asof(next_open)
        payload = {
            "open_ts":          next_open,
            "open_time_str":    time.strftime("%H:%M:%S", time.gmtime(next_open)) + " UTC",
            "time_until_open":  max(0, int(next_open - now)),
            "predicted_dir":    None,
            "confidence":       0.0,
            "score":            0.0,
            "breakdown":        {},
            "signal_computed":  False,
        }
        if result:
            payload.update({
                "predicted_dir":   result["direction"],
                "confidence":      result["confidence"],
                "score":           result["score"],
                "breakdown":       result["breakdown"],
                "signal_computed": True,
            })
        with self._lock:
            self.upcoming = payload

    def _signal_asof(self, open_ts: int):
        """只用 open_ts 之前的价格和 K 线打分。数据不够时返回 None。"""
        price = self._candles.last_price_before(open_ts)
        if price <= 0:
            return None
        return self._signal.compute(
            self._candles_asof("5m", open_ts, 50),
            self._candles_asof("1h", open_ts, 48),
            price,
            self._candles.get_prices_between(open_ts - PREOPEN_SLOPE_SEC, open_ts),
        )

    def _candles_asof(self, tf: str, open_ts: int, count: int) -> list:
        """留下开场前已存在的 K 线。未收盘的那根，收盘价改写成开场前最后一笔。"""
        interval = CandleEngine.TF_SECONDS[tf]
        price = self._candles.last_price_before(open_ts)
        kept = []
        for candle in self._candles.get_candles(tf, count + 2):
            if candle["open_time"] >= open_ts:
                continue
            candle = dict(candle)
            if candle["open_time"] + interval > open_ts and price > 0:
                candle["close"] = round(price, 2)
            kept.append(candle)
        return kept[-count:]

    def _ensure_opening_price(self, open_ts: int):
        """开盘基准只取开场前 60 秒的成交均价。没有开场前成交就不填。"""
        with self._lock:
            if self.current.get("opening_price", 0.0) > 0:
                return

        prices = self._candles.get_prices_between(open_ts - TWAP_WINDOW_SEC, open_ts)
        if not prices:
            return
        twap = sum(p for _, p in prices) / len(prices)
        cl_p = self._chainlink.price
        if twap <= 0:
            return
        with self._lock:
            self.current["opening_price"] = round(twap, 2)
            self.current["chainlink_open"] = round(cl_p, 2)
            open_ts_saved = self.current.get("open_ts")
            opening = self.current["opening_price"]
            chainlink_open = self.current["chainlink_open"]
        if open_ts_saved:
            self._db_save_opening(open_ts_saved, opening, chainlink_open)

    def _settle_round_internal(self, open_ts: int):
        """结算指定的一局"""
        close_ts = open_ts + ROUND_DURATION_SEC
        closes = self._candles.get_prices_between(close_ts - TWAP_WINDOW_SEC, close_ts)
        if closes:
            closing_price = sum(p for _, p in closes) / len(closes)
        else:
            closing_price = self._candles.last_price_before(close_ts)

        opening_price = self.current.get("opening_price", 0.0)
        predicted = self.current.get("predicted_dir")
        actual_dir = None
        is_correct = None
        if opening_price > 0 and closing_price > 0:
            actual_dir = "UP" if closing_price >= opening_price else "DOWN"
            if predicted in ("UP", "DOWN"):
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
        con = sqlite3.connect(self._db_path)
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
            "SELECT * FROM rounds ORDER BY open_ts DESC LIMIT 200"
        ).fetchall()
        cols = [d[1] for d in con.execute("PRAGMA table_info(rounds)").fetchall()]
        con.close()
        loaded = []
        for raw in reversed(rows):
            item = dict(zip(cols, raw))
            item["open_time_str"] = time.strftime("%H:%M:%S", time.gmtime(item["open_ts"])) + " UTC"
            if item.get("actual_dir"):
                item["phase"] = "SETTLED"
            loaded.append(item)
        with self._lock:
            self.history = loaded

    def _db_insert_round(self, open_ts: int):
        try:
            con = sqlite3.connect(self._db_path)
            con.execute(
                "INSERT OR IGNORE INTO rounds (open_ts, created_at) VALUES (?, ?)",
                (open_ts, _now_ms()))
            con.commit(); con.close()
        except Exception: pass

    def _db_load_prediction(self, open_ts: int):
        """同一局重启时，沿用已经落库的开场前预测，避免用开场后的价格重算。"""
        try:
            con = sqlite3.connect(self._db_path)
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM rounds WHERE open_ts=?", (open_ts,)
            ).fetchone()
            con.close()
        except Exception:
            return None
        if not row or not row["predicted_dir"]:
            return None
        return {
            "predicted_dir":   row["predicted_dir"],
            "confidence":      row["confidence"] or 0.0,
            "score":           row["score"] or 0.0,
            "signal_locked":   True,
            "signal_computed": True,
            "opening_price":   row["opening_price"] or 0.0,
            "chainlink_open":  row["chainlink_open"] or 0.0,
            "breakdown": {
                "macro":     row["score_macro"] or 0.0,
                "rsi":       row["score_rsi"] or 0.0,
                "bb":        row["score_bb"] or 0.0,
                "momentum":  row["score_momentum"] or 0.0,
                "micro":     row["score_micro"] or 0.0,
            },
        }

    def _db_save_prediction(self, snap: dict):
        bd = snap.get("breakdown") or {}
        try:
            con = sqlite3.connect(self._db_path)
            con.execute("""
                UPDATE rounds SET
                    predicted_dir=?, confidence=?, score=?,
                    score_macro=?, score_rsi=?, score_bb=?,
                    score_momentum=?, score_micro=?
                WHERE open_ts=?
            """, (
                snap.get("predicted_dir"),
                snap.get("confidence"),
                snap.get("score"),
                bd.get("macro"),
                bd.get("rsi"),
                bd.get("bb"),
                bd.get("momentum"),
                bd.get("micro"),
                snap.get("open_ts"),
            ))
            con.commit(); con.close()
        except Exception: pass

    def _db_save_opening(self, open_ts: int, opening: float, chainlink_open: float):
        try:
            con = sqlite3.connect(self._db_path)
            con.execute("""
                UPDATE rounds SET opening_price=?, chainlink_open=?
                WHERE open_ts=? AND (opening_price IS NULL OR opening_price=0)
            """, (opening, chainlink_open, open_ts))
            con.commit(); con.close()
        except Exception: pass

    def _db_settle(self, open_ts: int, closing: float, cl_close: float,
                   actual: str, is_correct):
        try:
            con = sqlite3.connect(self._db_path)
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
