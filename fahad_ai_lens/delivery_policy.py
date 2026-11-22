"""Pure V2 delivery policy. Not wired to the live receiver."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError

SIGNAL_TTL_SECONDS = 300
MAX_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (2, 5, 12)


def signal_id(packet):
    parts = [str(packet[key]) for key in ("symbol", "timeframe", "time", "event")]
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def expiry_time(packet, ttl_seconds=SIGNAL_TTL_SECONDS):
    candle = datetime.fromisoformat(packet["time"].replace("Z", "+00:00"))
    if candle.tzinfo is None:
        raise ValueError("candle time must include timezone")
    return candle.astimezone(timezone.utc) + timedelta(seconds=ttl_seconds)


def expired(packet, now=None, ttl_seconds=SIGNAL_TTL_SECONDS):
    now = now or datetime.now(timezone.utc)
    return now >= expiry_time(packet, ttl_seconds)


def retry_policy(stage, error, attempt, packet, now=None):
    """Return (action, delay). Ambiguous Telegram delivery is never auto-retried.

    urllib timeout/connection errors after Telegram POST may mean delivered.
    Claude failures can be retried while the signal remains fresh.
    """
    if expired(packet, now):
        return ("expired_stale", None)
    if stage == "telegram" and isinstance(error, (TimeoutError, URLError)):
        return ("ambiguous_sent", None)
    if isinstance(error, HTTPError):
        retryable = error.code == 429 or 500 <= error.code < 600
        if stage == "telegram" and error.code == 429:
            retryable = True
    else:
        retryable = stage != "telegram" and isinstance(error, (TimeoutError, URLError))
    if not retryable or attempt >= MAX_ATTEMPTS:
        return ("failed", None)
    return ("retry", RETRY_DELAYS_SECONDS[attempt - 1])
