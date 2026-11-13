"""Regression cases for source consistency, market phase and momentum horizons."""
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.services import fahad_compact_report as digest

NOW = datetime(2026, 11, 13, 19, 0, tzinfo=timezone.utc)


def candles(index):
    values = 100 + np.arange(len(index)) * 0.02
    return pd.DataFrame({'Open': values, 'High': values + 0.05,
                         'Low': values - 0.05, 'Close': values + 0.01,
                         'Volume': np.full(len(index), 100.0)}, index=index)


@pytest.fixture(scope='module')
def schedule():
    return digest.sessions(NOW)


@pytest.fixture(scope='module')
def raw(schedule):
    frames = {}
    for key, minutes, days in [('5m', 5, 5), ('1h', 60, 65)]:
        indexes = [pd.date_range(row['open'], row['close'], freq=f'{minutes}min',
                                 inclusive='left') for _, row in schedule.tail(days).iterrows()]
        index = indexes[0].append(indexes[1:])
        frames[key] = candles(index)
    frames['1d'] = candles(pd.DatetimeIndex(schedule.index[-100:]))
    return frames


def test_completed_horizons_and_no_open_candle_leak(raw, schedule):
    result = digest.analyze_symbol('NVDA', NOW, schedule, lambda _: raw)
    assert set(result['frames']) == {'5m', '15m', '1h', '4h', '1d'}
    assert result['frames']['5m']['as_of'] == '2026-11-13T19:00:00+00:00'
    assert result['frames']['4h']['as_of'] == '2026-11-13T18:30:00+00:00'
    assert result['frames']['1d']['as_of'] == '2026-11-12T21:00:00+00:00'
    changed = {k: v.copy() for k, v in raw.items()}
    # The 19:00 candle is still open at 19:00 and must not influence a signal.
    changed['5m'].loc[pd.Timestamp(NOW), ['Open', 'Close', 'High']] = 99999
    other = digest.analyze_symbol('NVDA', NOW, schedule, lambda _: changed)
    assert other['frames'] == result['frames']


def test_stale_missing_and_duplicate_symbols_stay_visible(raw):
    stale = {k: v.iloc[:-90] for k, v in raw.items()}
    text, records = digest.build_digest(['NVDA', 'BAD', 'NVDA'], NOW,
                                        lambda s: stale if s == 'NVDA' else {})
    assert len(records) == 2
    assert text.count('NVDA |') == 1
    assert 'NVDA | ي: غير متاح | أ: غير متاح' in text
    assert 'BAD | ي: غير متاح | أ: غير متاح' in text
    assert 'nan' not in text.lower()


def test_aggregation_rejects_gaps(raw, schedule):
    bars = digest.complete_bars(raw['5m'], schedule, NOW, 5)
    latest = pd.Timestamp('2026-11-13T18:55Z')
    bars = bars.drop(latest)
    aggregated = digest.aggregate(bars, schedule, NOW, 5, 15)
    with pytest.raises(digest.Unavailable, match='stale'):
        digest.validate_fresh(aggregated, digest.expected_end(schedule, NOW, 15))


def test_volume_is_required_for_strong_daily_signal():
    block = {'direction': 1, 'rsi': 65, 'volume_ratio': 1.4}
    assert digest.horizon([block, block], True) == 'صاعد قوي'
    assert digest.horizon([block, dict(block, volume_ratio=0.5)], True) == 'صاعد ضعيف'
    assert 'حجم غير متاح' in digest.horizon([dict(block, volume_ratio=None)], True)
    assert digest.horizon([block, dict(block, direction=-1)], True) == 'متذبذب'


def test_percent_derived_from_same_close_series():
    frame = candles(pd.date_range('2026-10-01', periods=25, tz='UTC'))
    frame = digest.normalize(frame)
    frame['end'] = frame.index
    frame.iloc[-2, frame.columns.get_loc('close')] = 173.73
    frame.iloc[-1, frame.columns.get_loc('close')] = 166.19
    assert digest.metrics(frame)['change_pct'] == pytest.approx(-4.3400679215)


def test_closed_session_disables_daily_and_retains_weekly(raw):
    text, _ = digest.build_digest(['NVDA'], datetime(2026, 11, 14, 2, tzinfo=timezone.utc),
                                  lambda _: raw)
    assert 'السوق مغلق' in text
    assert 'NVDA | ي: مغلق | أ: صاعد قوي' in text


def test_calendar_failure_is_unknown_not_closed():
    with patch.object(digest, 'sessions', side_effect=RuntimeError('calendar offline')):
        text, _ = digest.build_digest(['NVDA'], NOW, lambda _: {})
    assert 'حالة السوق غير متاحة' in text
    assert 'ي: غير متاح' in text
    assert 'مغلق' not in text


def test_early_close_and_dst():
    now = datetime(2026, 11, 27, 19, tzinfo=timezone.utc)
    schedule = digest.sessions(now)
    assert schedule.iloc[-1]['close'] == pd.Timestamp('2026-11-27T18:00Z')
    assert digest.expected_end(schedule, now, 60) == pd.Timestamp('2026-11-27T18:00Z')
    # An early-close 3.5h session must not masquerade as a complete 4h candle.
    assert digest.expected_end(schedule, now, 240) == pd.Timestamp('2026-11-25T18:30Z')
    before = digest.sessions(datetime(2026, 10, 30, 20, tzinfo=timezone.utc))
    assert before.iloc[-1]['open'] == pd.Timestamp('2026-10-30T13:30Z')


@pytest.mark.parametrize('fault', ['nan', 'negative', 'duplicate', 'naive', 'off_grid'])
def test_invalid_source_fails_closed(raw, schedule, fault):
    frame = raw['5m'].copy()
    if fault == 'nan':
        frame.iloc[-1, 0] = np.nan
    elif fault == 'negative':
        frame.iloc[-1, 4] = -1
    elif fault == 'duplicate':
        frame.index = frame.index[:-1].append(frame.index[-2:-1])
    elif fault == 'naive':
        frame.index = frame.index.tz_localize(None)
    else:
        frame.index = frame.index + pd.Timedelta(minutes=1)
    with pytest.raises(digest.Unavailable):
        digest.complete_bars(frame, schedule, NOW, 5)


@pytest.mark.parametrize('no_notify, delivered, usable, expected', [
    (False, True, True, True), (False, False, True, False),
    (False, True, False, False), (True, False, True, True),
])
def test_job_saves_audit_sends_once_and_reports_failure(
        monkeypatch, tmp_path, no_notify, delivered, usable, expected):
    import json
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock

    config = SimpleNamespace(stock_list=['NVDA', 'SPX'], trading_day_check_enabled=True)
    sender = Mock()
    sender.send.return_value = delivered

    def save(text, filename):
        path = tmp_path / filename
        path.write_text(text, encoding='utf-8')
        return str(path)

    sender.save_report_to_file.side_effect = save
    monkeypatch.setitem(sys.modules, 'src.config', SimpleNamespace(get_config=lambda: config))
    monkeypatch.setitem(sys.modules, 'src.notification', SimpleNamespace(NotificationService=lambda: sender))
    record = {'symbol': 'NVDA', 'frames': {'1d': {'as_of': NOW.isoformat()}} if usable else {}}
    monkeypatch.setattr(digest, 'build_digest', lambda symbols: ('Arabic compact digest', [record]))
    assert digest.run(no_notify=no_notify, force_run=True) is expected
    assert sender.send.call_count == (0 if no_notify else 1)
    snapshot = json.loads(next(tmp_path.glob('*.json')).read_text())
    assert snapshot['records'] == [record]
    assert next(tmp_path.glob('*.md')).read_text() == 'Arabic compact digest'
