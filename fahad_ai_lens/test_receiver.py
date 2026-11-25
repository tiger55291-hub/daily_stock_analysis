import unittest
import json
from queue import Empty, Queue
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from fahad_ai_lens.receiver import failure_detail, probe_ai_review, validate, within_new_york_session
from fahad_ai_lens import receiver


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


class FakeoutWatchTests(unittest.TestCase):
    def watch(self, event="SHORT_FAKEOUT_WATCH"):
        is_long = event.startswith("LONG_")
        p = packet()
        p.update(event=event, symbol="CAPITALCOM:SPX500", timeframe="1",
                 fakeout=dict(level=7200, extreme=7197 if is_long else 7203,
                              close=7201 if is_long else 7199,
                              rsi=65 if is_long else 55, reference_rsi=60))
        return p

    def test_both_watch_directions(self):
        for event in ("LONG_FAKEOUT_WATCH", "SHORT_FAKEOUT_WATCH"):
            with self.subTest(event=event):
                self.assertIsNone(validate(self.watch(event)))

    def test_rejects_wrong_symbol_frame_and_divergence(self):
        for changes in (
            {"symbol": "NASDAQ:NVDA"},
            {"timeframe": "5"},
            {"event": "SHORT_SETUP"},
            {"fakeout": {"level": 7200, "extreme": 7203, "close": 7200, "rsi": 55, "reference_rsi": 60}},
            {"fakeout": {"level": 7200, "extreme": 7203, "close": 7199, "rsi": 61, "reference_rsi": 60}},
        ):
            with self.subTest(changes=changes):
                self.assertIsNotNone(validate(self.watch() | changes))

    def test_watch_is_persisted_and_sent_without_ai_or_trade_tracking(self):
        p = self.watch()
        handler = object.__new__(receiver.Handler)
        handler.path = "/webhook?token=local-test"
        body = json.dumps(p).encode()
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        handler.reply = Mock()
        jobs = Queue()
        with (patch.object(receiver, "WATCH_Q", jobs),
              patch.object(receiver, "WEBHOOK_SECRET", "local-test"),
              patch.object(receiver, "missing_configuration", return_value=[]),
              patch.object(receiver, "DURABLE_ENABLED", True),
              patch.object(receiver, "NEW_YORK_SESSION_ONLY", True),
              patch.object(receiver, "within_new_york_session", return_value=True),
              patch.object(receiver, "LEDGER") as ledger,
              patch.object(receiver, "CANDLE_Q") as candles,
              patch.object(receiver, "ai_decision") as ai,
              patch.object(receiver, "post_json", return_value={"result": {"message_id": 42}}) as telegram):
            handler.do_POST()
            handler.reply.assert_called_once_with(200, {"ok": True, "queued": True})
            row = ledger.insert.call_args.args[0]
            self.assertEqual(row["timeframe"], "1")
            self.assertEqual(row["event_type"], p["event"])
            item = jobs.get_nowait()
            with patch.object(jobs, "get", side_effect=[item, Empty]):
                with self.assertRaises(Empty):
                    receiver.worker(jobs)
            ai.assert_not_called()
            candles.put_nowait.assert_not_called()
            self.assertIn("مراقبة PUT", telegram.call_args.args[1]["text"])
            self.assertEqual(ledger.update.call_args.kwargs["status"], "sent")
            self.assertNotIn("outcome_status", ledger.update.call_args.kwargs)
            ledger.reconcile_trades.assert_not_called()


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
