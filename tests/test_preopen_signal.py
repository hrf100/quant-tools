"""开场前预测：开场后不得改写，且打分不能看见开场后的成交。"""

import os
import sqlite3
import tempfile
import unittest

from btc_strategy.candle_engine import CandleEngine
from btc_strategy.round_manager import RoundManager
from btc_strategy.signal_engine import SignalEngine


class _Chainlink:
    price = 100.0


def _result(direction):
    return {
        "direction": direction,
        "score": 40.0 if direction == "UP" else -40.0,
        "confidence": 40.0,
        "breakdown": {
            "macro": 25.0 if direction == "UP" else -25.0,
            "rsi": 0.0,
            "rsi_val": 50.0,
            "bb": 0.0,
            "bb_pct": 50.0,
            "momentum": 0.0,
            "micro": 15.0 if direction == "UP" else -15.0,
            "slope_usd_s": 0.3 if direction == "UP" else -0.3,
        },
    }


class SignalTests(unittest.TestCase):
    def test_preopen_slope_is_usd_per_second(self):
        engine = SignalEngine()
        hours = [{"close": 100.0} for _ in range(30)]
        fives = [{"open": 100.0, "close": 100.0, "volume": 1.0}]
        prices = [(i * 6.0, 100.0 + i * 6.0) for i in range(11)]
        result = engine.compute(fives, hours, 110.0, prices)
        self.assertEqual(result["breakdown"]["macro"], 25.0)
        self.assertEqual(result["breakdown"]["micro"], 25.0)
        self.assertEqual(result["direction"], "UP")

    def test_short_window_does_not_score_slope(self):
        engine = SignalEngine()
        prices = [(float(i), 100.0 + i) for i in range(10)]
        micro, slope = engine._layer_micro(prices)
        self.assertEqual(micro, 0.0)
        self.assertEqual(slope, 0.0)


class RoundLockTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._dir.name, "rounds.db")
        self.candles = CandleEngine()
        self.rounds = RoundManager(self.candles, _Chainlink(), db_path=self.db)
        self.calls = []
        self.direction = "UP"

        def compute(c5, c1h, price, prices):
            self.calls.append({
                "price": price,
                "opens": [c["open_time"] for c in c5],
                "max_ts": max((t for t, _ in prices), default=None),
            })
            return _result(self.direction)

        self.rounds._signal.compute = compute

    def tearDown(self):
        self._dir.cleanup()

    def _feed(self, start, end, price):
        t = start
        while t < end:
            self.candles.on_tick(price, 0.01, t)
            t += 1.0

    def test_midround_start_has_no_call_for_current_round(self):
        self._feed(960, 980, 111.0)
        self.rounds._tick(980)
        current = self.rounds.get_current()
        self.assertIsNone(current["predicted_dir"])
        self.assertEqual(current["phase"], "NO_CALL")
        self.assertEqual(current["opening_price"], 0.0)
        upcoming = self.rounds.get_upcoming()
        self.assertEqual(upcoming["open_ts"], 1200)
        self.assertEqual(upcoming["predicted_dir"], "UP")

    def test_direction_freezes_at_open_and_ignores_later_ticks(self):
        self._feed(1100, 1200, 111.0)
        self.candles.on_tick(999.0, 0.01, 1200.5)
        self.rounds._tick(1201)
        locked = [c for c in self.calls if c["price"] == 111.0][-1]
        self.assertLess(locked["max_ts"], 1200)
        self.assertEqual(locked["price"], 111.0)
        self.assertTrue(all(open_ts < 1200 for open_ts in locked["opens"]))

        current = self.rounds.get_current()
        self.assertEqual(current["predicted_dir"], "UP")
        self.assertEqual(current["opening_price"], 111.0)
        self.assertTrue(current["signal_locked"])

        self.direction = "DOWN"
        self.candles.on_tick(50.0, 0.01, 1250)
        self.rounds._tick(1250)
        self.assertEqual(self.rounds.get_current()["predicted_dir"], "UP")
        self.assertEqual(self.rounds.get_upcoming()["predicted_dir"], "DOWN")

    def test_restart_keeps_the_preopen_call(self):
        self._feed(1100, 1200, 111.0)
        self.rounds._tick(1201)
        restarted = RoundManager(CandleEngine(), _Chainlink(), db_path=self.db)
        restarted._signal.compute = lambda *args: _result("DOWN")
        restarted._tick(1250)
        self.assertEqual(restarted.get_current()["predicted_dir"], "UP")
        self.assertEqual(restarted.get_current()["opening_price"], 111.0)

        con = sqlite3.connect(self.db)
        row = con.execute(
            "SELECT predicted_dir, opening_price FROM rounds WHERE open_ts=1200"
        ).fetchone()
        con.close()
        self.assertEqual(row, ("UP", 111.0))

    def test_settlement_uses_preopen_call_only(self):
        self._feed(840, 900, 100.0)
        self.rounds._tick(950)
        self.assertEqual(self.rounds.get_current()["predicted_dir"], "UP")
        self._feed(1140, 1200, 90.0)
        self.direction = "DOWN"
        self.rounds._tick(1201)
        settled = self.rounds.get_history(1)[0]
        self.assertEqual(settled["predicted_dir"], "UP")
        self.assertEqual(settled["actual_dir"], "DOWN")
        self.assertFalse(settled["is_correct"])
        stats = self.rounds.get_stats()
        self.assertEqual(stats["called_rounds"], 1)
        self.assertEqual(stats["correct"], 0)
        self.assertEqual(stats["accuracy_pct"], 0.0)

    def test_clear_history_keeps_the_live_round(self):
        self._feed(840, 900, 100.0)
        self.rounds._tick(950)
        self._feed(1140, 1200, 90.0)
        self.rounds._tick(1201)
        keep = self.rounds.get_current()["open_ts"]
        self.assertEqual(self.rounds.get_stats()["called_rounds"], 1)
        self.rounds.clear_history()
        self.assertEqual(self.rounds.get_stats()["settled_rounds"], 0)
        self.assertEqual(self.rounds.get_history(50), [])
        self.assertEqual(self.rounds.get_current()["predicted_dir"], "UP")
        con = sqlite3.connect(self.db)
        rows = con.execute("SELECT open_ts FROM rounds").fetchall()
        con.close()
        self.assertEqual(rows, [(keep,)])


if __name__ == "__main__":
    unittest.main()
