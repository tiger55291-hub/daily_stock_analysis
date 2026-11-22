"""Normalize closed TradingView candles and sent trade levels for the ledger."""
import math
from datetime import datetime, timedelta, timezone

BAR = timedelta(minutes=5)


def stamp(raw):
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return value.astimezone(timezone.utc) if value.tzinfo else None
    except ValueError:
        return None


def positive(value):
    return type(value) in (int, float) and 0 < value < math.inf


def tracking_fields(packet):
    if packet.get("event") not in ("LONG_SETUP", "SHORT_SETUP") or str(packet.get("timeframe")) != "5":
        return {}
    prices = packet.get("trade") or {}
    entry, stop, target = (prices.get(k) for k in ("entry", "stop", "target_1"))
    if not all(positive(v) for v in (entry, stop, target)):
        return {}
    ordered = stop < entry < target if packet["event"] == "LONG_SETUP" else target < entry < stop
    if not ordered or stamp(packet.get("time")) is None:
        return {}
    return dict(outcome_status="open", entry_price=entry, stop_price=stop, target_1=target)


def candle_batch(packet, tickers, now=None):
    if not isinstance(packet, dict) or packet.get("schema") != "ai-agent-lens/v1":
        return None
    symbol = packet.get("symbol")
    if (not isinstance(symbol, str) or len(symbol) > 128 or ":" not in symbol
            or symbol.rsplit(":", 1)[-1].upper() not in tickers or str(packet.get("timeframe")) != "5"):
        return None
    now = now or datetime.now(timezone.utc)
    current = stamp(packet.get("time"))
    if current is None or not now - timedelta(minutes=30) <= current <= now + timedelta(seconds=5):
        return None
    history = packet.get("recent_candles")
    recent = {}
    if isinstance(history, list) and len(history) <= 10:
        for row in history:
            if not isinstance(row, dict) or type(row.get("bars_ago")) is not int:
                continue
            offset = row["bars_ago"]
            when = stamp(row.get("time"))
            if 0 <= offset <= 9 and when and (when < current if offset else when == current):
                recent[offset] = (when, row.get("h"), row.get("l"))
    top = packet.get("candle")
    if isinstance(top, dict):
        recent[0] = (current, top.get("high"), top.get("low"))
    bars = {}
    for offset, (closed, high, low) in recent.items():
        if not positive(high) or not positive(low) or low > high:
            continue
        following = recent.get(offset + 1)
        previous = following[0] if following and following[0] < closed else None
        bars[closed] = dict(closed_at=closed.isoformat(),
                            previous_at=(previous or closed - BAR).isoformat(),
                            previous_verified=previous is not None, high=high, low=low)
    return (symbol, list(bars.values())) if bars else None
