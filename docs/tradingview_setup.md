# TradingView Setup Guide

## Adding the Pine Script Indicator

### Step 1 — Open Pine Editor
1. Open [TradingView](https://tradingview.com) and log in
2. Open any chart (recommended: NSE:NIFTY or NSE:BANKNIFTY, 15m timeframe)
3. At the bottom of the chart, click **Pine Editor**
4. Clear the default code

### Step 2 — Paste the Indicator
1. Open `tradingview/smc_connector.pine` from this repository
2. Select all content (Ctrl+A / Cmd+A)
3. Paste into Pine Editor
4. Click **Add to chart**

### Step 3 — Verify Drawings
After adding to chart, you should see:
- 🟩 Green boxes: Bullish Order Blocks (extending right until mitigated)
- 🟥 Red boxes: Bearish Order Blocks
- 🔵 Blue labels: BOS (Break of Structure)
- 🟠 Orange labels: CHOCH (Change of Character)
- ▲ ▼ Small triangles above/below candles: Swing High/Low
- ⚡ BSL / SSL dotted horizontal lines
- 📦 Session boxes: Asia (blue), London (yellow), NY (purple)
- Shaded zones: Premium (red tint above equilibrium), Discount (green tint below)

If you see none of these, try:
- Zoom out to see more bars
- Check that you are on a chart with enough history
- Right-click indicator → Settings → verify settings

### Step 4 — Configure Indicator Settings
Right-click indicator → Settings:

| Setting | Default | Description |
|---------|---------|-------------|
| Swing Lookback | 3 | Bars each side to confirm swing high/low |
| Show Session Boxes | On | Asia / London / NY boxes |
| Show Premium/Discount | On | Shaded zone above/below equilibrium |
| Show PDH/PDL | On | Previous day high/low dashed lines |
| Min OB Strength | 1 | Stars to display (1=all, 3=strongest only) |
| Show Entry/SL/TP | On | Lines when signal received |

### Step 5 — Create Webhook Alerts

You need one alert per signal type per instrument. Start with:
- NIFTY 15m — OB Entry
- BANKNIFTY 15m — OB Entry

**Creating an alert:**
1. Right-click on chart → **Add Alert**
2. **Condition**: Select **SMC Connector** from first dropdown
3. **Condition type**: Select the specific signal (e.g., "Bullish OB Entry")
4. Alert name: e.g., `SMC NIFTY 15m Bullish OB`
5. **Expiration**: Set to far future (1 year)

**Setting up webhook:**
1. In the alert dialog, go to **Notifications** tab
2. Check **Webhook URL**
3. Enter your Railway webhook URL:
   `https://YOUR-APP.up.railway.app/webhook`

**Setting the message JSON:**
In the **Message** field, replace the default text with:

```json
{
  "secret": "YOUR_TV_WEBHOOK_SECRET_HERE",
  "instrument": "{{ticker}}",
  "exchange": "{{exchange}}",
  "timeframe": "{{interval}}",
  "signal_type": "OB_ENTRY",
  "direction": "LONG",
  "structure": "CHOCH",
  "ob_high": {{high}},
  "ob_low": {{low}},
  "ob_mid": 0,
  "fvg_present": false,
  "liquidity_swept": false,
  "session": "INDIA",
  "htf_bias": "BULLISH",
  "in_discount": true,
  "trap_present": false,
  "volume_ratio": 1.0,
  "close": {{close}},
  "timestamp": "{{timenow}}"
}
```

**Important:**
- Replace `YOUR_TV_WEBHOOK_SECRET_HERE` with the value you set in Railway
- The `{{ticker}}`, `{{close}}`, etc. are TradingView variables — leave them as-is
- Change `"direction": "LONG"` to `"SHORT"` for bearish alerts
- Create separate alerts for SHORT signals

### Step 6 — Alert Types to Create

For comprehensive coverage, create these alert conditions:

| Alert | Direction | Signal Type | Notes |
|-------|-----------|-------------|-------|
| Bullish OB Entry | LONG | OB_ENTRY | Main long signal |
| Bearish OB Entry | SHORT | OB_ENTRY | Main short signal |
| Bullish BOS | LONG | BOS | Trend continuation |
| Bearish BOS | SHORT | BOS | Trend continuation |
| Bullish CHOCH | LONG | CHOCH | Reversal signal |
| Bearish CHOCH | SHORT | CHOCH | Reversal signal |
| Bullish Sweep | LONG | SWEEP | After SSL taken |
| Bearish Sweep | SHORT | SWEEP | After BSL taken |

### Step 7 — Recommended Timeframe Combinations

**Intraday (default setting):**
- Entry chart: 15m
- Context: 1H + 4H for bias
- Works for: Stocks, F&O, Indices

**Scalp:**
- Entry chart: 5m
- Context: 15m + 1H
- Works for: Index options intraday

**Swing:**
- Entry chart: 4H or Daily
- Context: Weekly
- Works for: Stocks, swing F&O positions

### Step 8 — Test the Connection

1. After creating an alert, wait for market hours
2. Or manually trigger by running an alert test (Pro plan feature)
3. Check Railway logs immediately:
   - Go to Railway dashboard → your service → Logs
   - You should see: `Webhook received: NSE:NIFTY 15m OB_ENTRY LONG`
4. Check Telegram — if signal passes all filters, you receive a message

### Dashboard Panel (Top Right of Chart)

The indicator shows a live dashboard:
```
┌─────────────────────────┐
│ SMC Dashboard           │
│ Session: London 2h 14m  │
│ Daily Bias: ↑ BULLISH   │
│ Bull OBs: 3 | Bear: 2   │
│ Unmitigated FVGs: 4     │
│ Last: A+ LONG 14:22     │
│ Traps Today: 1          │
└─────────────────────────┘
```

### Troubleshooting TradingView

**Alert not firing:**
- Check alert is Active (clock icon in Alerts panel is green)
- Verify the condition is triggered (add to chart and watch)
- TradingView free plan limits 1 alert. Upgrade to Basic+ or Pro for webhooks

**Webhook not received:**
- Test URL with curl: `curl -X GET https://your-app.up.railway.app/health`
- Verify `secret` in JSON matches `TV_WEBHOOK_SECRET` in Railway exactly
- Check Railway logs for any rejected webhook messages

**Too many signals:**
- Increase `MIN_CONFLUENCE_SCORE` in Railway variables (e.g., 78 or 82)
- Set quality filter to `APLUS_ONLY` via `/markets` in Telegram
- Reduce active timeframes in your alerts

**Wrong segment detected:**
- Ensure `"exchange"` field in JSON is correct (e.g., `NSE`, `MCX`, `BINANCE`)
- Check `segment_detector.py` logic in docs if needed

### TradingView Plan Requirements

| Feature | Required Plan |
|---------|--------------|
| Webhook alerts | Basic+ or higher |
| Multiple alerts | Pro or higher recommended |
| Pine Script indicators | All plans |
| Real-time data (NSE) | Depends on exchange subscription |

For Indian markets (NSE/BSE), you need the NSE data subscription on TradingView.
