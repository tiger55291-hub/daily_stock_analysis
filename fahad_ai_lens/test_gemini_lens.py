import json
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from queue import Queue
from unittest.mock import Mock, patch
from urllib.error import URLError

from fahad_ai_lens import gemini_lens as gemini, receiver


def candle_packet(now):
    def bar(time, opened, high, low, close):
        return dict(time=time.isoformat(), open=opened, high=high, low=low, close=close)
    return dict(schema=gemini.SCHEMA, symbol=gemini.SYMBOL, time=now.isoformat(), levels=[7200, 7204],
                bars=[bar(now - gemini.MINUTE, 7202, 7205, 7201, 7203),
                      bar(now, 7201, 7205, 7199, 7204)])


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        self.packet = candle_packet(self.now)

    def test_bounce_picks_touched_level_and_deterministic_exit(self):
        call = gemini.bounce(self.packet, gemini.minute_batch(self.packet, self.now))
        self.assertEqual((call["side"], call["context"]["level"]), ("CALL", 7200))
        self.assertEqual((call["entry"], call["stop"], call["target"]), (7204, 7198.9, 7211.65))

        self.packet["bars"][0].update(open=7196, low=7195, high=7198, close=7197)
        self.packet["bars"][1].update(open=7199, low=7196, high=7201, close=7197)
        put = gemini.bounce(self.packet, gemini.minute_batch(self.packet, self.now))
        self.assertEqual((put["side"], put["context"]["level"]), ("PUT", 7200))
        self.assertLess(put["target"], put["entry"])
        self.assertGreater(put["stop"], put["entry"])

    def test_no_bounce_without_levels_or_contiguous_previous_candle(self):
        self.packet["levels"] = []
        self.assertIsNone(gemini.bounce(self.packet, gemini.minute_batch(self.packet, self.now)))
        self.packet["levels"] = [7200]
        self.packet["bars"][0]["time"] = (self.now - 2 * gemini.MINUTE).isoformat()
        self.assertIsNone(gemini.bounce(self.packet, gemini.minute_batch(self.packet, self.now)))

    def test_invalid_symbol_or_stale_candle_rejected(self):
        self.assertIsNone(gemini.minute_batch(self.packet | {"symbol": "CBOE:SPX"}, self.now))
        self.assertIsNone(gemini.minute_batch(self.packet, self.now + 6 * gemini.MINUTE))

    def test_fakeout_under_current_level(self):
        event = dict(event="SHORT_FAKEOUT_WATCH", time=self.now.isoformat(),
                     fakeout=dict(level=7200, close=7199, extreme=7201.5))
        setup = gemini.fakeout(event)
        self.assertEqual((setup["pattern"], setup["side"], setup["stop"]), ("FAKEOUT", "PUT", 7201.6))

    def test_gemini_only_accepts_matching_structured_verdict(self):
        candidate = gemini.bounce(self.packet, gemini.minute_batch(self.packet, self.now))
        for decision, expected in (("CALL", "ارتداد"), ("PUT", None), ("WAIT", None)):
            result = dict(candidates=[dict(content=dict(parts=[
                dict(text=json.dumps({"decision": decision, "reason": "ارتداد"}))
            ]))])
            api = Mock(return_value=result)
            self.assertEqual(gemini.review(candidate, api, "fake-model", "test-key"), expected)
            self.assertEqual(api.call_args.kwargs["headers"], {"x-goog-api-key": "test-key"})
        self.assertIsNone(gemini.review(candidate, Mock(return_value={}), "fake-model", "test-key"))

    def test_delivery_is_independent_deduplicated_and_tracks_only_sent(self):
        candidate = gemini.bounce(self.packet, gemini.minute_batch(self.packet, self.now))
        ledger = Mock()
        ledger.insert_gemini.side_effect = [True, False]
        reviewer = Mock(return_value="ارتداد")
        telegram = Mock(return_value={"result": {"message_id": 14}})
        gemini.review_and_deliver(candidate, ledger, reviewer, telegram)
        gemini.review_and_deliver(candidate, ledger, reviewer, telegram)
        telegram.assert_called_once_with(candidate, "ارتداد")
        reviewer.assert_called_once_with(candidate)
        self.assertEqual(ledger.update_gemini.call_args.kwargs["status"], "sent")
        ledger.reconcile_gemini.assert_called_once()

        ledger.reset_mock(side_effect=True)
        telegram.reset_mock()
        telegram.side_effect = URLError("unknown delivery")
        gemini.review_and_deliver(candidate, ledger, reviewer, telegram)
        telegram.assert_called_once()
        self.assertEqual(ledger.update_gemini.call_args.kwargs["status"], "ambiguous")
        ledger.reconcile_gemini.assert_not_called()


class RoutingTests(unittest.TestCase):
    def request(self, path, packet=None):
        handler = object.__new__(receiver.Handler)
        handler.path = path
        handler.reply = Mock()
        if packet is None:
            handler.headers = {"Authorization": "Bearer local"}
        else:
            body = json.dumps(packet).encode()
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = BytesIO(body)
        return handler

    def test_gemini_candles_bypass_claude_and_five_minute_ledger(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        sender = self.request("/webhook/gemini?token=local", candle_packet(now))
        candles, jobs = Queue(), Queue()
        with (patch.object(receiver, "WEBHOOK_SECRET", "local"),
              patch.object(receiver, "gemini_ready", return_value=True),
              patch.object(receiver, "NEW_YORK_SESSION_ONLY", True),
              patch.object(receiver, "within_new_york_session", return_value=True),
              patch.object(receiver, "GEMINI_CANDLES", candles),
              patch.object(receiver, "GEMINI_JOBS", jobs),
              patch.object(receiver, "candle_batch") as five_minute,
              patch.object(receiver, "missing_configuration") as legacy_config):
            sender.do_POST()
        sender.reply.assert_called_once_with(200, {"ok": True, "queued": True})
        five_minute.assert_not_called()
        legacy_config.assert_not_called()
        self.assertEqual(len(candles.get_nowait()), 2)
        self.assertEqual(jobs.get_nowait()["pattern"], "GAMMA_BOUNCE")

    def test_fakeout_still_queued_for_original_watch_and_separately_for_gemini(self):
        now = datetime.now(timezone.utc).isoformat()
        packet = dict(schema="ai-agent-lens/v1", symbol=gemini.SYMBOL, timeframe="1", time=now,
                      event="SHORT_FAKEOUT_WATCH", fakeout=dict(level=7200, close=7199, extreme=7201,
                                                               rsi=55, reference_rsi=60))
        handler = self.request("/webhook?token=local", packet)
        watches, gems = Queue(), Queue()
        with (patch.object(receiver, "WEBHOOK_SECRET", "local"),
              patch.object(receiver, "missing_configuration", return_value=[]),
              patch.object(receiver, "DURABLE_ENABLED", True),
              patch.object(receiver, "NEW_YORK_SESSION_ONLY", False),
              patch.object(receiver, "LEDGER"),
              patch.object(receiver, "WATCH_Q", watches),
              patch.object(receiver, "GEMINI_JOBS", gems),
              patch.object(receiver, "gemini_ready", return_value=True)):
            handler.do_POST()
        self.assertEqual(watches.get_nowait()["packet"], packet)
        self.assertEqual(gems.get_nowait()["pattern"], "FAKEOUT")

    def test_stats_uses_only_gemini_ledger_and_original_route_keeps_contract(self):
        for route, method in (("/stats/gemini", "gemini_stats"), ("/stats", "trade_stats")):
            handler = self.request(route + "?period=weekly")
            with (patch.object(receiver, "WEBHOOK_SECRET", "local"),
                  patch.object(receiver, "DURABLE_ENABLED", True),
                  patch.object(receiver, "LEDGER") as ledger):
                getattr(ledger, method).return_value = [{"wins": 2}]
                handler.do_GET()
                getattr(ledger, method).assert_called_once_with("weekly")
                self.assertEqual(len(ledger.method_calls), 1)
            handler.reply.assert_called_once_with(200, {"ok": True, "timezone": "America/New_York",
                                                        "rows": [{"wins": 2}]})
