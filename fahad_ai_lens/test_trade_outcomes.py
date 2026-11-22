from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

from fahad_ai_lens import receiver
from fahad_ai_lens.trade_outcomes import candle_batch, tracking_fields


class OutcomeTests(unittest.TestCase):
    now = datetime(2026, 11, 23, 14, 35, tzinfo=timezone.utc)
    packet = {"schema": "ai-agent-lens/v1", "symbol": "CAPITALCOM:SPX500", "timeframe": "5",
              "time": "2026-11-23T14:35:00Z", "event": "BAR_CLOSE",
              "candle": {"high": 6915, "low": 6901}}

    def test_legacy_packet_keeps_inferred_predecessor(self):
        _, bars = candle_batch(self.packet, {"SPX500"}, self.now)
        self.assertEqual(bars[0]["previous_at"], "2026-11-23T14:30:00+00:00")
        self.assertFalse(bars[0]["previous_verified"])

    def test_backfill_uses_actual_close_across_a_session_break(self):
        old = {"bars_ago": 1, "time": "2026-11-20T21:00:00Z", "h": 6911, "l": 6900}
        _, bars = candle_batch(dict(self.packet, recent_candles=[old]), {"SPX500"}, self.now)
        current = next(b for b in bars if b["closed_at"] == self.now.isoformat())
        self.assertEqual(current["previous_at"], "2026-11-20T21:00:00+00:00")
        self.assertTrue(current["previous_verified"])

    def test_only_valid_sent_trade_levels_are_eligible(self):
        trade = {"entry": 6900, "stop": 6890, "target_1": 6910}
        self.assertEqual(tracking_fields(dict(self.packet, event="LONG_SETUP", trade=trade))["outcome_status"], "open")
        self.assertEqual(tracking_fields(dict(self.packet, event="LONG_RETEST_WATCH", trade=trade)), {})
        self.assertEqual(tracking_fields(dict(self.packet, event="SHORT_SETUP", trade=trade)), {})
        self.assertEqual(tracking_fields(dict(self.packet, event="SHORT_SETUP",
                                              trade=dict(entry=6900, stop=6910, target_1=6890)))["outcome_status"], "open")

    def test_stats_requires_authorization_and_supports_weekly(self):
        handler = Mock(path="/stats?period=weekly", headers={})
        ledger = Mock()
        ledger.trade_stats.return_value = [{"symbol": "CAPITALCOM:SPX500", "side": "PUT", "wins": 1}]
        with patch.multiple(receiver, WEBHOOK_SECRET="fixture", DURABLE_ENABLED=True, LEDGER=ledger):
            receiver.Handler.do_GET(handler)
            handler.reply.assert_called_once_with(401, {"ok": False})
            ledger.trade_stats.assert_not_called()
            handler.headers["Authorization"] = "Bearer fixture"
            receiver.Handler.do_GET(handler)
            ledger.trade_stats.assert_called_once_with("weekly")
            self.assertEqual(handler.reply.call_args.args[0], 200)
