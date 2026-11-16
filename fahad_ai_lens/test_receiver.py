import unittest
from datetime import datetime, timedelta, timezone

from fahad_ai_lens.receiver import validate


def packet():
    return {
        "schema": "ai-agent-lens/v1",
        "event": "LONG_SETUP",
        "symbol": "CBOE:SPX",
        "timeframe": "5",
        "time": datetime.now(timezone.utc).isoformat(),
        "regime": "TREND_UP",
        "scores": {"long": 85, "short": 20},
        "trend": {"htf_bias": "UP"},
        "trade": {"entry": 7000.0, "stop": 6988.0, "target_1": 7018.0, "target_2": 7036.0},
    }


class ValidateTests(unittest.TestCase):
    def test_valid_signal(self):
        self.assertIsNone(validate(packet()))

    def test_wrong_schema(self):
        p = packet()
        p["schema"] = "nope"
        self.assertEqual(validate(p), "bad schema")

    def test_below_score(self):
        p = packet()
        p["scores"]["long"] = 60
        self.assertEqual(validate(p), "score below minimum")

    def test_higher_timeframe_conflict(self):
        p = packet()
        p["trend"]["htf_bias"] = "DOWN"
        self.assertEqual(validate(p), "HTF not aligned")

    def test_expired_packet(self):
        p = packet()
        p["time"] = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        self.assertEqual(validate(p), "stale or future packet")

    def test_trade_update_ignored(self):
        p = packet()
        p["event"] = "TRADE_UPDATE"
        self.assertEqual(validate(p), "not a new setup")

    def test_range_ignored(self):
        p = packet()
        p["regime"] = "RANGE"
        self.assertEqual(validate(p), "unfavorable regime")

    def test_high_volatility_boundary(self):
        for event, bias, key in (("LONG_SETUP", "UP", "long"), ("SHORT_SETUP", "DOWN", "short")):
            for score in (84, 85, 90, 100):
                with self.subTest(event=event, score=score):
                    p = packet()
                    p.update(event=event, regime="HIGH_VOLATILITY")
                    p["trend"]["htf_bias"] = bias
                    p["scores"][key] = score
                    self.assertEqual(validate(p), None if score >= 85 else "score below minimum")
            p["trend"]["htf_bias"] = "DOWN" if bias == "UP" else "UP"
            self.assertEqual(validate(p), "HTF not aligned")

    def test_missing_prices(self):
        p = packet()
        p["trade"]["stop"] = None
        self.assertEqual(validate(p), "missing prices")

    def test_wrong_ticker(self):
        p = packet()
        p["symbol"] = "NASDAQ:AAPL"
        self.assertEqual(validate(p), "ticker not selected")


if __name__ == "__main__":
    unittest.main()
