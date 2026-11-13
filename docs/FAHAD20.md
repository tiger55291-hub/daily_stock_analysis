# FAHAD20 compact Actions digest

This fork's daily Actions job sends one Arabic message with every configured
`STOCK_LIST` symbol. `full` and `stocks-only` use this mode by default. The
`market-only` input retains the original market-review behavior. The existing
schedule is unchanged (10:00 UTC, weekdays); this is before the US regular session,
so daily momentum is marked closed. Run the workflow manually during the regular
session to inspect intraday momentum. This is a snapshot job, not a streaming bot.

## Output and scope

Each row contains the symbol, daily-horizon momentum (`ي`) and weekly-horizon
trend (`أ`), with `[support/resistance]` when the required candles are available.
These are measurements of the underlying, not option trade instructions or
validated probability estimates. Strike, expiry, bid/ask spread, implied
volatility and Greeks are not evaluated. No gamma-bot repository or formula is
changed. No verified news is included in this compact version; missing news is
never interpreted as an absence of market events.

- Daily horizon: completed regular-session 5m and aggregated 15m candles. Both
  directions must agree. EMA9/EMA21 order and EMA21 slope over three bars define
  direction; otherwise the result is mixed. Strong momentum additionally requires
  RSI14 >=55 (up) or <=45 (down) on both frames, plus each frame's latest volume
  >=1.2 times its preceding 20-bar mean. Missing/zero-base volume is explicitly
  unavailable, never strong. This volume comparison is not time-of-day adjusted.
- Weekly horizon: agreement of 1h, 4h and daily directions; the same RSI thresholds
  distinguish strong from weak. This is a multi-timeframe trend description,
  not a guarantee for a weekly option expiry.
- Support/resistance: min low / max high of the last 20 completed 15m candles
  for the daily horizon and 4h candles for the weekly horizon. These are recent
  ranges, not pivot detection or GEX.
- 15m and 4h candles are anchored at the US regular open. Aggregates containing
  gaps are rejected. The 2.5h closing hourly stub is not treated as a full 4h bar.
  Holidays, DST and early closes use the XNYS exchange calendar.
- At least 25 complete bars are required for every used frame. Each latest bar
  must end exactly at its calendar-derived latest completed interval. Stale,
  incomplete, missing, unordered, non-finite or unverified-timezone inputs are
  rejected. A missing symbol remains in the list as unavailable. Daily momentum
  is disabled outside the regular session. Yahoo candles may still be delayed;
  this source does not certify a real-time feed suitable for 0DTE execution.

## Configuration and rollback

The workflow sets `FAHAD_COMPACT_REPORT` from the repository Actions Variable,
with default `true`. Set that Variable to `false` to restore the original full
LLM reports and market recap. This option is a workflow switch, not a setting
that modifies the upstream UI/API report formats. Existing notification secrets
and `STOCK_LIST` are reused; put credentials in Actions Secrets only.

Compact mode bypasses LLM generation and the separate long market recap. It does
not consume Gemini tokens. Legacy mode still uses the existing model settings.
Other providers may have their own usage terms. No new paid service is provisioned.

Local invocation (using existing runtime dependencies and `.env`):

```bash
python -m src.services.fahad_compact_report --no-notify
python -m src.services.fahad_compact_report --force-run
python -m pytest tests/test_fahad_compact_report.py -q
```

`--force-run` allows historical output on a non-trading day, never relabels it as
current. `--no-notify` saves the report without sending. Actions retains the
Markdown message and matching timestamped JSON source metrics/limitations under
`reports/` in its existing report artifact for 30 days. Percent changes in the
audit are derived from the same frame's two closes; legacy LLM percentages are
not reused. A total data outage or notification failure produces a failing job,
while still exposing the unavailable-data message when delivery is possible.
