"""OpenAI shadow strategy: first five-minute FVG retest with confirmed 4h structure."""
from datetime import timedelta

if __package__:
    from .trade_outcomes import stamp
else:
    from trade_outcomes import stamp


def bias(bars):
    highs, lows = [], []
    for left, mid, right in zip(bars, bars[1:], bars[2:]):
        if any(stamp(b["bucket"]) - stamp(a["bucket"]) != timedelta(hours=4)
               for a, b in ((left, mid), (mid, right))):
            continue
        if mid["high"] > max(left["high"], right["high"]):
            highs.append(mid["high"])
        if mid["low"] < min(left["low"], right["low"]):
            lows.append(mid["low"])
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            return "CALL"
        if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            return "PUT"


def candidate(minutes, opened, closed, side):
    if side not in ("CALL", "PUT"):
        return
    prices = {stamp(b["closed_at"]): b for b in minutes}
    bars = []
    for offset in range(0, (closed - opened) // timedelta(minutes=1), 5):
        group = [prices.get(opened + timedelta(minutes=offset+i)) for i in range(1, 6)]
        if any(b is None for b in group):
            bars.clear()
            continue
        bars.append(dict(open=group[0]["open"], close=group[-1]["close"],
                         high=max(b["high"] for b in group), low=min(b["low"] for b in group),
                         time=group[-1]["closed_at"]))
    if len(bars) < 4 or stamp(bars[-1]["time"]) != closed:
        return
    last = bars[-1]
    bullish = side == "CALL"
    for i in range(len(bars)-2, 1, -1):
        first, third = bars[i-2], bars[i]
        lo, hi = (first["high"], third["low"]) if bullish else (third["high"], first["low"])
        if hi <= lo:
            continue
        intervening = bars[i+1:-1]
        if any(b["low"] <= hi if bullish else b["high"] >= lo for b in intervening):
            continue
        if last["low"] > hi or last["high"] < lo:
            continue
        price = last["close"]
        if bullish and price > max(hi, last["open"]):
            stop, target = min(lo, last["low"]) - 0.1, price + 10
        elif not bullish and price < min(lo, last["open"]):
            stop, target = max(hi, last["high"]) + 0.1, price - 10
        else:
            continue
        return dict(candle_time=closed.isoformat(), gap_time=third["time"], side=side,
                    range_low=lo, range_high=hi, entry=price, stop=round(stop, 4),
                    target=round(target, 4), candle=last, pattern="FVG_RETEST", bias_4h=side)
