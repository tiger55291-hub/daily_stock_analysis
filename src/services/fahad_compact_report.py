"""Deterministic US watchlist digest; never turn LLM prose into market facts.

The two horizons describe underlying momentum, not option contract selection.
Only complete regular-session candles are used. Missing data stays missing.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
import logging
import numpy as np
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger(__name__)
NY = ZoneInfo('America/New_York')
RIYADH = ZoneInfo('Asia/Riyadh')


class Unavailable(ValueError):
    """A required source block is absent, invalid, incomplete, or stale."""


@lru_cache(maxsize=1)
def calendar():
    import exchange_calendars as xcals
    return xcals.get_calendar('XNYS')


def sessions(now):
    schedule = calendar().schedule
    dates = pd.to_datetime(schedule.index).date
    today = now.astimezone(NY).date()
    if today < min(dates) or today > max(dates):
        raise Unavailable('calendar coverage')
    schedule = schedule[(dates <= today)].tail(140).copy()
    schedule['open'] = pd.to_datetime(schedule['open'], utc=True)
    schedule['close'] = pd.to_datetime(schedule['close'], utc=True)
    if schedule.empty:
        raise Unavailable('calendar')
    return schedule


def normalize(frame):
    if frame is None or frame.empty:
        raise Unavailable('empty')
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise Unavailable('unverified timestamps')
    frame = frame.rename(columns=lambda c: str(c).lower()).copy()
    columns = ['open', 'high', 'low', 'close', 'volume']
    if not set(columns).issubset(frame.columns):
        raise Unavailable('columns')
    frame = frame[columns].apply(pd.to_numeric, errors='coerce')
    if (frame.isna().any().any() or not np.isfinite(frame.to_numpy()).all()
            or (frame[['open', 'high', 'low', 'close']] <= 0).any().any()
            or (frame['volume'] < 0).any()
            or (frame['high'] < frame[['open', 'close', 'low']].max(axis=1)).any()
            or (frame['low'] > frame[['open', 'close', 'high']].min(axis=1)).any()):
        raise Unavailable('invalid OHLCV')
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise Unavailable('unordered timestamps')
    return frame


def complete_bars(frame, schedule, now, minutes):
    frame = normalize(frame)
    if frame.index.tz is None:
        raise Unavailable('unverified timezone')
    frame.index = frame.index.tz_convert('UTC')
    parts = []
    for _, session in schedule.iterrows():
        opened, closed = session['open'], session['close']
        part = frame[(frame.index >= opened) & (frame.index < closed)].copy()
        if part.empty:
            continue
        if any((t - opened) % pd.Timedelta(minutes=minutes) != pd.Timedelta(0) for t in part.index):
            raise Unavailable('unaligned candles')
        part['end'] = [min(t + pd.Timedelta(minutes=minutes), closed) for t in part.index]
        part = part[part['end'] <= pd.Timestamp(now)]
        parts.append(part)
    if not parts:
        raise Unavailable('no complete regular candles')
    return pd.concat(parts).sort_index()


def aggregate(frame, schedule, now, source_minutes, target_minutes):
    """Session-open anchored bins; discard gaps and short closing stubs."""
    rows = []
    for _, session in schedule.iterrows():
        start = session['open']
        while start + pd.Timedelta(minutes=target_minutes) <= min(session['close'], pd.Timestamp(now)):
            end = start + pd.Timedelta(minutes=target_minutes)
            part = frame[(frame.index >= start) & (frame.index < end)]
            expected = pd.date_range(start, end, freq=f'{source_minutes}min', inclusive='left')
            if (len(part) == len(expected) and part.index.equals(expected)
                    and (part['end'] <= end).all()):
                rows.append(dict(time=start, end=end, open=part['open'].iloc[0],
                                 high=part['high'].max(), low=part['low'].min(),
                                 close=part['close'].iloc[-1], volume=part['volume'].sum()))
            start = end
    if not rows:
        raise Unavailable('incomplete aggregate')
    return pd.DataFrame(rows).set_index('time')


def expected_end(schedule, now, minutes):
    for _, session in schedule.iloc[::-1].iterrows():
        if session['open'] >= pd.Timestamp(now):
            continue
        ends = []
        start = session['open']
        while start < session['close']:
            end = min(start + pd.Timedelta(minutes=minutes), session['close'])
            # Four-hour series intentionally excludes the 2.5-hour closing stub.
            if end <= pd.Timestamp(now) and (minutes != 240 or end - start == pd.Timedelta(hours=4)):
                ends.append(end)
            start += pd.Timedelta(minutes=minutes)
        if ends:
            return max(ends)
    raise Unavailable('no expected candle')


def validate_fresh(frame, expected):
    if len(frame) < 25:
        raise Unavailable('history too short')
    end = frame['end'].iloc[-1]
    if end > expected or expected != end:
        raise Unavailable('stale candle')
    return frame


def daily_bars(frame, schedule, now):
    frame = normalize(frame)
    dates = pd.to_datetime(frame.index).date
    completed = schedule[schedule['close'] <= pd.Timestamp(now)]
    closes = {pd.Timestamp(d).date(): row['close'] for d, row in completed.iterrows()}
    keep = [d in closes for d in dates]
    frame = frame[keep].copy()
    frame['end'] = [closes[d] for d, ok in zip(dates, keep) if ok]
    if completed.empty:
        raise Unavailable('no closed daily session')
    return validate_fresh(frame, completed['close'].iloc[-1])


def metrics(frame):
    close = frame['close']
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    slope = ema21.iloc[-1] - ema21.iloc[-4]
    direction = (1 if close.iloc[-1] > ema9.iloc[-1] > ema21.iloc[-1] and slope > 0 else
                 -1 if close.iloc[-1] < ema9.iloc[-1] < ema21.iloc[-1] and slope < 0 else 0)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean().iloc[-1]
    rsi = 50 if gain == loss == 0 else 100 if loss == 0 else 100 - 100/(1 + gain/loss)
    base_volume = frame['volume'].iloc[-21:-1].mean()
    volume_ratio = float(frame['volume'].iloc[-1]/base_volume) if base_volume > 0 else None
    change = float((close.iloc[-1]/close.iloc[-2] - 1)*100)
    return dict(direction=direction, rsi=float(rsi), volume_ratio=volume_ratio,
                change_pct=change, close=float(close.iloc[-1]),
                support=float(frame['low'].iloc[-20:].min()),
                resistance=float(frame['high'].iloc[-20:].max()),
                as_of=frame['end'].iloc[-1].isoformat())


def horizon(blocks, volume_required=False):
    directions = [block['direction'] for block in blocks]
    if not directions or len(set(directions)) != 1 or directions[0] == 0:
        return 'متذبذب'
    direction = directions[0]
    rsi_ok = all(b['rsi'] >= 55 if direction == 1 else b['rsi'] <= 45 for b in blocks)
    volume_ok = all(b['volume_ratio'] is not None and b['volume_ratio'] >= 1.2 for b in blocks)
    if volume_required and any(b['volume_ratio'] is None for b in blocks):
        return ('صاعد' if direction == 1 else 'هابط') + '؛ حجم غير متاح'
    strong = rsi_ok and (volume_ok if volume_required else True)
    return ('صاعد' if direction == 1 else 'هابط') + (' قوي' if strong else ' ضعيف')


def fetch_frames(symbol):
    import yfinance as yf
    from data_provider.us_index_mapping import get_us_index_yf_symbol, is_us_stock_code
    mapped, _ = get_us_index_yf_symbol(symbol)
    if not mapped and not is_us_stock_code(symbol):
        raise Unavailable('US symbols only')
    ticker = yf.Ticker(mapped or symbol)
    frames = {}
    for key, interval, period in [('5m', '5m', '5d'), ('1h', '1h', '3mo'), ('1d', '1d', '6mo')]:
        try:
            frames[key] = ticker.history(interval=interval, period=period, auto_adjust=True,
                                         prepost=False, actions=False, timeout=10, raise_errors=True)
        except Exception as exc:
            logger.warning('Compact source unavailable for %s/%s: %s', symbol, key, type(exc).__name__)
            frames[key] = None
    return frames


def analyze_symbol(symbol, now, schedule, loader=fetch_frames):
    result = {'symbol': symbol, 'source': 'Yahoo Finance', 'frames': {}}
    try:
        raw = loader(symbol)
    except Exception as exc:
        result['error'] = type(exc).__name__
        return result
    for key in ('5m', '1h', '1d'):
        try:
            if key == '1d':
                bars = daily_bars(raw.get(key), schedule, now)
            else:
                minutes = 5 if key == '5m' else 60
                bars = complete_bars(raw.get(key), schedule, now, minutes)
                bars = validate_fresh(bars, expected_end(schedule, now, minutes))
            result['frames'][key] = metrics(bars)
            if key != '1d':
                target = 15 if key == '5m' else 240
                agg = aggregate(bars, schedule, now, minutes, target)
                agg = validate_fresh(agg, expected_end(schedule, now, target))
                result['frames']['15m' if key == '5m' else '4h'] = metrics(agg)
        except (Unavailable, ValueError, TypeError, AttributeError) as exc:
            result.setdefault('limitations', {})[key] = str(exc)
    return result


def render(records, now, opened):
    phase = ('حالة السوق غير متاحة' if opened is None else 'الجلسة مفتوحة' if opened
             else 'السوق مغلق — الأسبوعي من شموع مكتملة تاريخية')
    lines = ['FAHAD20 | ' + now.astimezone(RIYADH).strftime('%d/%m %H:%M') + ' السعودية', phase]
    for record in records:
        frames = record['frames']
        day = 'مغلق' if opened is False else 'غير متاح'
        if opened and all(k in frames for k in ('5m', '15m')):
            day = horizon([frames['5m'], frames['15m']], volume_required=True)
        week = horizon([frames[k] for k in ('1h', '4h', '1d')]) if all(k in frames for k in ('1h', '4h', '1d')) else 'غير متاح'
        def levels(key):
            b = frames.get(key)
            return f" [{b['support']:.2f}/{b['resistance']:.2f}]" if b else ''
        lines.append(f"{record['symbol']} | ي: {day}{levels('15m') if opened and day != 'غير متاح' else ''} | أ: {week}{levels('4h') if week != 'غير متاح' else ''}")
    asofs = [b['as_of'] for r in records for b in r['frames'].values()]
    if asofs:
        times = [datetime.fromisoformat(t).astimezone(RIYADH) for t in asofs]
        lines.append('توقيت الشموع: ' + min(times).strftime('%d/%m %H:%M') + '–'
                     + max(times).strftime('%d/%m %H:%M') + ' السعودية')
    lines += ['ي: زخم 5 و15د | أ: اتجاه 1س و4س ويومي',
              '[دعم/مقاومة] من آخر 20 شمعة؛ ليست قاما.',
              'Yahoo قد يتأخر؛ الشموع الناقصة/القديمة تُستبعد.',
              'تقييم للأصل؛ لم تُفحص سيولة الأوبشن أو السترايك.',
              'الأخبار: لا يتضمن هذا الملخص أخبارًا موثقة.']
    return '\n'.join(lines)


def build_digest(symbols, now=None, loader=fetch_frames):
    fixed_now = now
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now must be timezone aware')
    symbols = list(dict.fromkeys(symbols))
    def safe_load(symbol):
        try:
            return loader(symbol)
        except Exception as exc:
            logger.warning('Compact load failed for %s: %s', symbol, type(exc).__name__)
            return {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        raw = dict(zip(symbols, pool.map(safe_load, symbols)))
    # Check freshness at publication time, including time spent collecting data.
    now = fixed_now or datetime.now(timezone.utc)
    try:
        schedule = sessions(now)
    except Exception as exc:
        logger.warning('Compact calendar unavailable: %s', type(exc).__name__)
        records = [{'symbol': s, 'frames': {}, 'error': 'calendar unavailable'} for s in symbols]
        return render(records, now, None), records
    opened = any(row['open'] <= pd.Timestamp(now) < row['close'] for _, row in schedule.iterrows())
    with ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(lambda s: analyze_symbol(s, now, schedule, lambda symbol: raw[symbol]), symbols))
    return render(records, now, opened), records


def run(*, no_notify=False, force_run=False):
    """Run the fork's compact job using existing config and notification routes."""
    import json
    from pathlib import Path
    from src.config import get_config
    from src.notification import NotificationService

    config = get_config()
    symbols = config.stock_list
    if not symbols:
        raise ValueError('STOCK_LIST is empty')
    now = datetime.now(timezone.utc)
    if config.trading_day_check_enabled and not force_run:
        schedule = sessions(now)
        today = now.astimezone(NY).date()
        if today not in pd.to_datetime(schedule.index).date:
            logger.info('US non-trading day; use --force-run for historical digest')
            return True
    text, records = build_digest(symbols)
    notifier = NotificationService()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = Path(notifier.save_report_to_file(text, f'fahad20_{stamp}.md'))
    path.with_suffix('.json').write_text(
        json.dumps({'generated_at': stamp, 'records': records}, ensure_ascii=False, indent=2,
                   allow_nan=False), encoding='utf-8')
    usable = any(r['frames'] for r in records)
    delivered = True if no_notify else notifier.send(text, email_stock_codes=symbols)
    # Missing data is still disclosed, but a data outage must not look successful.
    return usable and delivered


if __name__ == '__main__':
    import argparse
    from src.config import setup_env
    setup_env()
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-notify', action='store_true')
    parser.add_argument('--force-run', action='store_true')
    args = parser.parse_args()
    raise SystemExit(0 if run(no_notify=args.no_notify, force_run=args.force_run) else 1)
