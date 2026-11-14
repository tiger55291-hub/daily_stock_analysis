# FAHAD20 | AI Agent Lens — SPX 5m (experimental)

This is the replacement plan for the **analysis output** of \`@Fahad20AnalysisBot\`, not for FAHAD GEX or any other bot.

## Files
- \`ai-agent-lens-fahad.pine\`: adapted [AI Agent Lens](https://github.com/roan-maverick/tradingview-indicator-for-ai-agents) by roan-maverick, MIT licensed.
- \`receiver.py\`: independent TradingView webhook receiver. Filters an SPX setup, reviews it with a language model (Gemini API), and sends CALL / PUT alerts through the existing Telegram bot account. It **never** places broker orders.
- \`test_receiver.py\`: checks packet validation.

## Chart configuration
1. Open **CBOE:SPX**, **5 minutes**, in TradingView.
2. Paste \`ai-agent-lens-fahad.pine\` into Pine Editor, Save, Add to chart.
3. Default settings: score 80/100, higher time frame 15 minutes, compact panel, minimal drawings, packet mode "Setups and trade updates."
4. Make one alert: **FAHAD AI Lens (SPX 5m) → Any alert() function call**.
5. Under Notifications enable **Webhook URL**: \`https://YOUR-RENDER-URL/webhook?token=YOUR_WEBHOOK_SECRET\`. Leave the message field unchanged. TradingView's script sends JSON at candle close. Keep two-factor authentication enabled in TradingView.
6. The receiver only forwards fresh **LONG_SETUP / SHORT_SETUP** events when score >=80, 15-minute direction agrees, and regime is not a range/high-volatility spike. The model can still choose WAIT. Each accepted packet can produce at most one telegram signal per server session.

## Hosting (separate Render web service)
Use a new **Python** web service connected to the repository and branch \`experiment/fahad20-ai-lens\`:
- Build command: \`true\` (Python standard library only)
- Start command: \`python -u fahad_ai_lens/receiver.py\`
- Health check: \`/health\`
- Add environment variables **in Render**, not to GitHub/public files:
  - \`WEBHOOK_SECRET\` = long random shared secret
  - \`TELEGRAM_BOT_TOKEN\` = token for the **existing** \`@Fahad20AnalysisBot\` (not a new bot)
  - \`TELEGRAM_CHAT_ID\` = destination chat ID; the bot must be allowed to message it
  - \`GEMINI_API_KEY\` = Google AI Studio Gemini API key
  - Optional \`GEMINI_MODEL\` (default \`gemini-2.5-flash\`), \`MIN_SCORE\` (default \`80\`), \`TIMEFRAME\` (default \`5\`), \`ALLOWED_TICKERS\` (default \`SPX,SPX500\`)
  
Once deployed, verify \`GET /health\`, then use a test TradingView alert to verify webhook delivery and bot messaging.

### Important
The previous scheduled **Daily Stock Analysis** GitHub Actions workflow \`.github/workflows/00-daily-analysis.yml\` remains unchanged on the default branch. **Disable its schedule only after this service has passed end-to-end verification**, to avoid stopping all messages during migration. This pilot never touches the FAHAD GEX calculations.

### Notes
- Price levels in Telegram are **underlying SPX values**, not option strike prices or option premium quotes.
- The AI score describes confluence; it is **not** a calibrated win probability.
- Raw JSON packets can contain malformed or misleading data. The receiver checks the schema, timeframe, symbol, freshness and numeric levels before analysis.
- This model API is Gemini; it does **not** use an imaginary “Claude Fable 5.2” API.
- The bot token, Gemini key, chat ID and live Render/TradingView settings cannot be inferred from a GitHub repository; they must be configured securely.
