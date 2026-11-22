import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from fahad_ai_lens.delivery_policy import expired, retry_policy, signal_id


def packet(minutes_ago=1):
    return dict(symbol="CAPITALCOM:SPX500", timeframe="5",
                time=(datetime.now(timezone.utc)-timedelta(minutes=minutes_ago)).isoformat(),
                event="LONG_SETUP")


class DeliveryPolicyTests(unittest.TestCase):
    def test_stable_identity(self):
        p = packet()
        self.assertEqual(signal_id(p), signal_id(dict(reversed(list(p.items())))))
        self.assertNotEqual(signal_id(p), signal_id({**p, "event": "SHORT_SETUP"}))

    def test_expiration(self):
        self.assertFalse(expired(packet(1)))
        self.assertTrue(expired(packet(6)))

    def test_retry_claude(self):
        self.assertEqual(retry_policy("claude", URLError("offline"), 1, packet())[0], "retry")

    def test_telegram_timeout_ambiguous(self):
        self.assertEqual(retry_policy("telegram", URLError("timeout"), 1, packet())[0], "ambiguous_sent")

    def test_expired_not_retried(self):
        self.assertEqual(retry_policy("claude", URLError("offline"), 1, packet(6))[0], "expired_stale")

    def test_retry_cap(self):
        self.assertEqual(retry_policy("claude", URLError("offline"), 3, packet())[0], "failed")

    def test_telegram_rate_limit(self):
        err = HTTPError("https://api.telegram.org", 429, "rate limit", {}, None)
        self.assertEqual(retry_policy("telegram", err, 1, packet())[0], "retry")


if __name__ == "__main__":
    unittest.main()
