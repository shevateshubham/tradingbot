# Telegram Bot Setup Guide

## Creating Your Bot via BotFather

1. Open Telegram (app or web.telegram.org)
2. Search for **@BotFather** in the search bar
3. Tap on it and click **Start**
4. Send the command: `/newbot`
5. BotFather asks for a **name** — this is the display name:
   - Example: `SMC Trading Signals`
6. BotFather asks for a **username** — this is the @handle:
   - Must end in `bot`
   - Example: `smc_signals_myname_bot`
   - Must be unique globally
7. BotFather replies with your **bot token**:
   ```
   Done! Congratulations on your new bot. You will find it at t.me/smc_signals_myname_bot.
   Use this token to access the HTTP API:
   7123456789:AAF-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
8. **Copy this token immediately** — this is your `TELEGRAM_BOT_TOKEN`
9. Add it to Railway Variables

## Getting Your Chat ID

1. Open Telegram
2. Search for **@userinfobot**
3. Send `/start`
4. It responds with your user info including:
   ```
   Your ID: 987654321
   First name: Your Name
   ```
5. Copy the number after **Your ID:** — this is your `TELEGRAM_CHAT_ID`
6. Add it to Railway Variables

## Testing the Connection

After deploying to Railway and setting env vars:

1. Find your bot on Telegram (search for its username)
2. Click **Start** or send `/start`
3. The bot should respond with the market selection onboarding screen

If the bot doesn't respond:
- Check Railway logs for errors
- Verify `TELEGRAM_BOT_TOKEN` is correct (no extra spaces)
- Confirm the bot service is running (green status in Railway)

## Bot Commands Reference

| Command | What it does |
|---------|-------------|
| `/start` | Show market selection onboarding |
| `/markets` | View and edit all preferences |
| `/status` | Server health, uptime, active connections |
| `/active` | List open signals with current price vs levels |
| `/performance` | Last 30 days win rate, RR, net P&L |
| `/today` | Today's signals and outcomes so far |
| `/pause` | Pause all signals temporarily |
| `/resume` | Resume signals after pause |
| `/golive` | Switch from paper to live mode |
| `/setaccount 500000` | Update account size |
| `/rollback` | Show last 5 deployments, pick one to restore |
| `/approve N` | Approve weekly improvement PR number N |
| `/reject N` | Reject weekly improvement PR number N |
| `/help` | Full command list |

## Signal Message Format

When a signal is sent, it looks like this:

```
🎯 SMC SIGNAL — NIFTY [INDICES]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 Direction   : 🟢 LONG
⭐ Grade       : A+
🔢 Score       : 91/100
⏱ Timeframe   : 4H bias | 15m entry
📐 Setup       : Bullish OB + FVG | CHOCH confirmed
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💰 TRADE LEVELS
├─ Entry    : 22,450  (Limit Order)
├─ Stop Loss: 22,310  (140 pts | 0.62%)
├─ Target 1 : 22,590  (1:1 → move SL to entry)
├─ Target 2 : 22,730  (1:2 → take 50% off)
└─ Target 3 : 22,870  (1:3 → trail remainder)

📦 Lots       : 2 suggested
💸 Charges    : ₹847 estimated round trip
✅ Net at TP1 : ₹1,153 after all charges
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
...
```

## Notification Settings

To avoid missing signals, configure Telegram notifications:
1. Open your bot chat
2. Tap the bot name at the top → **Notifications**
3. Enable **Sound** and **Vibrate**
4. Disable "Mute" if it was set

For Telegram on mobile, ensure notifications are enabled in phone settings.

## Security Notes

- Your bot only responds to messages from your `TELEGRAM_CHAT_ID`
- Any other user sending messages to your bot is ignored
- Never share your `TELEGRAM_BOT_TOKEN` — it controls the bot completely
- If token is compromised: go to @BotFather → `/revoke` → generate new token → update Railway variable
