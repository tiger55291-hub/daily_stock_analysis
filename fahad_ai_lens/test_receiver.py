import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from fahad_ai_lens.receiver import failure_detail, probe_ai_review, validate, within_new_york_session


class NewYorkSessionTests(unittest.TestCase):
    def test_regular_session_uses_candle_close_and_new_york_dst(self):
        for timestamp, expected in [
            ("2026-11-25T14:30:00Z", False),
            ("2026-11-25T14:35:00Z", True),
            ("2026-11-25T21:00:00Z", True),
            ("2026-11-25T21:05:00Z", False),
            ("2026-07-06T13:35:00Z", True),
            ("2026-07-06T20:05:00Z", False),
        ]:
            with self.subTest(timestamp=timestamp):
                self.assertEqual(within_new_york_session({"time": timestamp}), expected)

    def test_thanksgiving_early_close_and_weekend(self):
        self.assertFalse(within_new_york_session({"time": "2026-11-26T16:00:00Z"}))
        self.assertTrue(within_new_york_session({"time": "2026-11-27T18:00:00Z"}))
        self.assertFalse(within_new_york_session({"time": "2026-11-27T18:05:00Z"}))
        self.assertFalse(within_new_york_session({"time": "2026-11-28T16:00:00Z"}))


class DiagnosticTests(unittest.TestCase):
    @patch("fahad_ai_lens.receiver.MODEL_PROVIDER", "anthropic")
    def test_credit_failure_logs_only_the_safe_classification(self):
        response = BytesIO(b'{"error":{"type":"invalid_request_error","message":"Your credit balance is too low; hidden details"}}')
        error = HTTPError("https://example.test/", 400, "Bad request", {}, response)
        self.assertEqual(failure_detail("ai_review", error), "ai_review:HTTP400:insufficient_credits")

    @patch("fahad_ai_lens.receiver.telegram_signal")
    @patch("fahad_ai_lens.receiver.telegram_pilot")
    @patch("fahad_ai_lens.receiver.ai_decision", return_value=None)
    def test_probe_never_sends_to_telegram(self, review, pilot, signal):
        with self.assertLogs("fahad20-ai-lens", level="INFO"):
            probe_ai_review()
        self.assertEqual(review.call_args.args[0]["event"], "HEALTH_CHECK")
        pilot.assert_not_called()
        signal.assert_not_called()


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

    def test_range_reaches_ai_validation(self):
        p = packet()
        p["regime"] = "RANGE"
        self.assertEqual(validate(p), None)

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
