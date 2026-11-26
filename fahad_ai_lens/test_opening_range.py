import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from fahad_ai_lens import opening_range, receiver


class OpeningRangeTests(unittest.TestCase):
    def setUp(self):
        self.opened = datetime(2026, 11, 25, 9, 30, tzinfo=ZoneInfo("America/New_York"))
        self.closed = self.opened + timedelta(minutes=16)
        self.bars = [self.bar(i, 7199, 7200, 7190, 7198) for i in range(1, 16)]

    def bar(self, offset, opened, high, low, close):
        return dict(closed_at=(self.opened + timedelta(minutes=offset)).isoformat(),
                    open=opened, high=high, low=low, close=close)

    def result(self):
        return opening_range.candidate(self.bars, self.opened, self.closed)

    def test_call_and_put_with_exact_risk_multiple(self):
        self.bars.append(self.bar(16, 7199, 7206, 7198, 7205))
        call = self.result()
        self.assertEqual((call["side"], call["stop"], call["target"]), ("CALL", 7197.9, 7215.65))
        self.bars[-1] = self.bar(16, 7191, 7192, 7184, 7185)
        put = self.result()
        self.assertEqual((put["side"], put["stop"], put["target"]), ("PUT", 7192.1, 7174.35))

    def test_wick_body_and_first_close_constraints(self):
        self.bars.append(self.bar(16, 7200, 7210, 7199, 7201))
        self.assertIsNone(self.result())
        self.bars.append(self.bar(17, 7201, 7209, 7200, 7208))
        self.closed += timedelta(minutes=1)
        self.assertIsNone(self.result())
        self.bars[-2] = self.bar(16, 7199, 7205, 7197, 7200)
        self.assertEqual(self.result()["side"], "CALL")

    def test_missing_opening_minute_is_not_interpolated(self):
        self.bars.pop(3)
        self.bars.append(self.bar(16, 7199, 7206, 7198, 7205))
        self.assertIsNone(self.result())

    def test_review_request_is_structured_and_does_not_store(self):
        api = Mock(return_value={"output": [{"content": [
            {"type": "output_text", "text": json.dumps({"decision": "WAIT", "reason": "ضعيف"})}
        ]}]})
        self.assertEqual(opening_range.review({}, api, "test-model", "test-key"), ("WAIT", "ضعيف"))
        self.assertFalse(api.call_args.args[1]["store"])
        self.assertTrue(api.call_args.args[1]["text"]["format"]["strict"])

    def test_wait_records_candidate_without_sending_telegram(self):
        self.bars.append(self.bar(16, 7199, 7206, 7198, 7205))
        ledger = Mock()
        ledger.opening_range_bars.return_value = self.bars
        with (patch.object(receiver, "LEDGER", ledger), patch.object(receiver, "expired", return_value=False),
              patch.object(opening_range, "review", return_value=("WAIT", "ضعيف")),
              patch.object(receiver, "post_json") as post):
            receiver.process_orb_candle(self.closed.isoformat())
        ledger.insert_orb.assert_called_once()
        ledger.update_orb.assert_called_once_with(self.result(), review_status="wait")
        post.assert_not_called()

    def test_default_disabled_even_with_gemini_enabled(self):
        with patch.object(receiver, "gemini_ready", return_value=True):
            self.assertFalse(receiver.orb_ready())
