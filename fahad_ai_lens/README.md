# FAHAD20 | AI Agent Lens — SPX 5m (experimental)

This is the replacement plan for the **analysis output** of `@Fahad20AnalysisBot`, not for FAHAD GEX or any other bot.

## Files
- `ai-agent-lens-fahad.pine`: adapted [AI Agent Lens](https://github.com/roan-maverick/tradingview-indicator-for-ai-agents) by roan-maverick, MIT licensed.
- `receiver.py`: independent TradingView webhook receiver. Filters an SPX setup, reviews it with Claude Fable 5.2 (or optionally Gemini), and sends CALL / PUT alerts through the existing Telegram bot account. It **never** places broker orders.
- `test_receiver.py`: checks packet validation.

## Chart configuration
1. Open **CBOE:SPX**, **5 minutes**, in TradingView.
2. Paste `ai-agent-lens-fahad.pine` into Pine Editor, Save, Add to chart.
3. Default settings: score 80/100, higher time frame 15 minutes, compact panel, minimal drawings, packet mode "Every bar close" (keeps the free receiver active during market hours; Telegram sends only qualified setups).
4. After a script change, update the chart script and recreate/update each alert snapshot; existing alerts retain their saved script version. Make one alert: **FAHAD AI Lens (SPX 5m) → Any alert() function call**.
5. Under Notifications enable **Webhook URL**: `https://YOUR-RENDER-URL/webhook?token=YOUR_WEBHOOK_SECRET`. Leave the message field unchanged. TradingView's script sends JSON at candle close. Keep two-factor authentication enabled in TradingView.
6. The receiver only forwards fresh **LONG_SETUP / SHORT_SETUP** events when score >=80, 15-minute direction agrees, and regime is not a range. High-volatility setups require score >=85 with aligned higher-timeframe direction. The model can still choose WAIT. Each accepted packet can produce at most one telegram signal per server session.

## SPX500 one-minute fakeout watch

Add `spx500-fakeout-watch.pine` as a separate private indicator on standard
`CAPITALCOM:SPX500` one-minute candles. Create a separate **Any alert() function
call** alert with the same webhook destination. Keep the existing five-minute
alerts running.

The watch compares one-minute RSI(14) at the confirmed five-minute swing
price with RSI at the new price extreme. It fires on the first one-minute
close back through the reference, within five minutes of the breach. A lower
RSI at a higher price produces a PUT watch. A higher RSI at a lower price
produces a CALL watch. The receiver applies the configured New York session
filter and sends directly to Telegram. Durable mode records watch delivery
in the signal ledger. Watches do not enter trade stats or five-minute candle
history.

## Hosting (separate Render web service)
Use a new **Python** web service connected to the repository and branch `experiment/fahad20-ai-lens`:
- Build command: `true` (Python standard library only)
- Start command: `python -u fahad_ai_lens/receiver.py`
- Health check: `/health`
- Add environment variables **in Render**, not to GitHub/public files:
  - `WEBHOOK_SECRET` = long random shared secret
  - `TELEGRAM_BOT_TOKEN` = token for the **existing** `@Fahad20AnalysisBot` (not a new bot)
  - `TELEGRAM_CHAT_ID` = destination chat ID; the bot must be allowed to message it
  - `MODEL_PROVIDER` = `anthropic` by default, or `gemini`
  - `ANTHROPIC_API_KEY` = Anthropic Claude API key (for `MODEL_PROVIDER=anthropic`)
  - `GEMINI_API_KEY` = Gemini API key (for the optional Gemini lane or `MODEL_PROVIDER=gemini`)
  - Optional `ANTHROPIC_MODEL` (default `claude-fable-5-2`), `GEMINI_MODEL` (default `gemini-2.5-flash`), `MIN_SCORE` (default `80`), `TIMEFRAME` (default `5`), `ALLOWED_TICKERS` (default `SPX,SPX500`)
  
Once deployed, verify `GET /health` shows `ready: true`, then use a test TradingView alert to verify webhook delivery and bot messaging. With missing secrets, the server safely reports `ready: false` and returns HTTP 503 to all webhook POSTs.

### Important
- Render Free goes idle after 15 minutes; TradingView can time out after 3 seconds. Every-bar JSON helps keep the receiver awake during trading, but opening-session alerts may still be missed. For reliable real-time operation, use an always-on service after testing.
The previous scheduled **Daily Stock Analysis** GitHub Actions workflow `.github/workflows/00-daily-analysis.yml` remains unchanged on the default branch. **Disable its schedule only after this service has passed end-to-end verification**, to avoid stopping all messages during migration. This pilot never touches the FAHAD GEX calculations.

### Notes
- Price levels in Telegram are **underlying SPX values**, not option strike prices or option premium quotes.
- The AI score describes confluence; it is **not** a calibrated win probability.
- Raw JSON packets can contain malformed or misleading data. The receiver checks the schema, timeframe, symbol, freshness and numeric levels before analysis.
- Claude Fable 5.2 is supported via Anthropic's official API. Claude consumer subscriptions do **not** automatically provide an API key.
- The bot token, Gemini key, chat ID and live Render/TradingView settings cannot be inferred from a GitHub repository; they must be configured securely.

## V2 trade outcomes

Only delivered 5-minute CALL and PUT setups with valid price levels enter the
statistics. Observation begins after the signal candle. T1 means a win, SL
means a loss, and touching both in one candle means a loss. Levels and results
are measured on the charted asset. The win rate uses closed observations.

The database migration in `supabase/migrations` stores the latest candle cursor
and the outcome in Supabase. Each incoming candle resumes pending observations.
Deploy the migration before deploying the receiver. Keep the existing alert
mode **Every bar close**. To enable recovery over missing messages and session
breaks, update the Pine script and recreate the TradingView alerts so recent
candles include their actual close times. Missing predecessor candles leave
the outcome open pending sufficient data. Older alerts can record consecutive
5-minute candles. The candle journal keeps 30 days per active symbol.

`GET /stats?period=daily` or `GET /stats?period=weekly` returns up to 300 recent
buckets for the selected period, grouped by New York signal date, exact symbol
and CALL/PUT. Send `Authorization: Bearer <WEBHOOK_SECRET>` in the request. The
private Supabase view `lens_trade_stats` also contains the saved aggregates.

## Parallel Gemini lane (experimental)

Keep `MODEL_PROVIDER=anthropic` and the existing Claude settings. Gemini is
off by default. To enable it, first apply `gemini_outcomes.sql` in Supabase,
configure `GEMINI_API_KEY` in Render, and set `GEMINI_LENS_ENABLED=true`.
`GEMINI_LENS_MODEL` can override the configured Gemini model for this lane.
Durable storage must be enabled.

Use the separate `spx500-gemini-candles.pine` indicator on standard
`CAPITALCOM:SPX500` one-minute candles. Select **Any alert() function call**
and point the new alert at `/webhook/gemini` with the existing webhook
credential. The indicator sends closed candles during New York hours and
for one hour after the regular close. Enter up to 10 comma-separated numeric
centers in the **SPX500** feed's price units. An empty input tracks candles
and skips gamma bounces. Update the alert's saved indicator settings when
changing the centers.

The original Fakeout watch keeps sending its monitoring message. An
independent queue sends the detected pattern to Gemini for a second,
explicitly labeled alert if Gemini approves. For a center bounce, a full
prior minute must stay on the approach side, then the next candle must
touch the level and close back on the approach side with a directional
body. The closest qualifying center is used. The experimental stop is
0.1 points beyond the excursion and T1 is 1.5 times initial price risk.
Only approvals matching the detected direction are sent to Telegram.

Use the same bearer authorization as for `/stats` with
`/stats/gemini?period=daily` or `?period=weekly`. Gemini results are grouped
by symbol, pattern and CALL/PUT. Observation uses up to 60 consecutive full
one-minute candles after Telegram delivery. SL takes precedence when both
levels are touched in one candle. Missing data are marked incomplete after
the backfill window. Win rate uses wins plus losses as the denominator.

## OpenAI opening-range pilot

Uses the same validated SPX500 1-minute candle stream as Gemini and a separate
review queue and ledger. Keep the existing TradingView alert. Apply
`opening_range_outcomes.sql` after the Gemini migration and configure
`OPENAI_API_KEY` as a Render secret before setting `OPENAI_ORB_ENABLED=true`.
The model can be configured with `OPENAI_ORB_MODEL`.

The regular New York session's first 15 closed minutes define the range.
Only the first close strictly beyond each side qualifies for evaluation.
The breakout candle must have a directional body of at least half its range
and close within the leading quarter. Missing minutes block evaluation until
backfilled. A close on the boundary does not count as a breakout.

The baseline enters notionally at the signal close, sets SL 0.1 points
beyond the other end of the signal candle and T1 at 1.5 times the SL distance.
Record all qualifying candidates before AI review. A matching OpenAI WATCH
sends a labeled monitoring alert to the existing Telegram chat. Outcomes
use the next 60 consecutive 1-minute candles with SL taking priority on a
dual hit. The stats separately count a close back inside the range within
five minutes and expose missing-data cases. Use the existing bearer auth
with `/stats/openai?period=daily` or `?period=weekly`. Review after 10 observed
sessions using these baseline counts and the subset sent to Telegram.

### OpenAI FVG shadow comparison
The independent SPX500 FVG lane uses completed 4-hour UTC buckets of 48 five-minute
bars. Two confirmed higher swing highs and lows define bullish bias, and two lower
highs and lows define bearish bias. One bar on either side confirms a swing.
The signal uses the first five-minute retest and directional rejection of a
three-bar gap formed during the same New York cash session. A missing minute
breaks the five-minute sequence. If multiple gaps qualify, the newest wins.
Review uses the configured OpenAI model. Results are paper observations with
a ten-point target, a stop 0.1 point beyond the gap/rejection extreme and a
60-minute window. A simultaneous stop and target on a minute bar counts as a
loss. Inspect authenticated GET /stats/openai/fvg for daily or weekly results
grouped by verdict. This lane uses the existing candle feeds and emits no
Telegram messages during evaluation.
