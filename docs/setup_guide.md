# SMC Trading Bot — Complete Setup Guide

Follow every step in order. Do not skip steps.
Total time: approximately 45–60 minutes.

---

## Step 1 — Create a Railway Account

1. Go to [railway.app](https://railway.app)
2. Click **Start a New Project**
3. Sign up with GitHub (recommended — enables auto-deploy)
4. Verify your email address
5. Go to **Account → Billing** and add a payment method
   - Railway charges ~$1–3/month for this bot
   - Free tier available but sleeps after inactivity — not suitable

---

## Step 2 — Create GitHub Account and Repository

1. Go to [github.com](https://github.com) and create an account if needed
2. Click **New Repository** (top right, `+` icon)
3. Repository name: `smc-trading-bot`
4. Visibility: **Private** (important — your webhook secret is used here)
5. Do NOT initialize with README (you'll push one)
6. Click **Create repository**
7. Note your repository URL: `https://github.com/YOURUSERNAME/smc-trading-bot`

### Set up branch protection for `main`:
1. Go to Settings → Branches → Add rule
2. Branch name pattern: `main`
3. Enable: **Require a pull request before merging**
4. Enable: **Require status checks to pass** → select `test`
5. Enable: **Do not allow force pushes**
6. Enable: **Do not allow deletions**
7. Click **Save changes**

### Create GitHub Personal Access Token:
1. Go to GitHub → Settings → Developer Settings → Personal Access Tokens → Tokens (classic)
2. Click **Generate new token (classic)**
3. Name: `smc-trading-bot-token`
4. Expiration: No expiration (or 1 year)
5. Scopes: Check `repo` (full repository access)
6. Click **Generate token** — copy and save it immediately (shown once only)

### Add token to GitHub Actions secrets:
1. Go to your repository → Settings → Secrets and variables → Actions
2. Click **New repository secret**
3. Name: `RAILWAY_TOKEN` (you'll get this value in Step 4)
4. Leave value blank for now — come back after Step 4

---

## Step 3 — Push Code to GitHub

Open terminal (or use Claude Code):

```bash
cd smc-trading-bot

git init
git add .
git commit -m "Initial commit: SMC trading bot"
git branch -M main

# Replace YOURUSERNAME with your GitHub username
git remote add origin https://github.com/YOURUSERNAME/smc-trading-bot.git
git push -u origin main

# Create the changes branch
git checkout -b changes
git push -u origin changes
git checkout main
```

Verify: Go to GitHub → your repository → Actions tab.
You should see the workflows listed (they won't run yet — that's fine).

---

## Step 4 — Set Up Railway Project

1. Go to [railway.app](https://railway.app) → **New Project**
2. Select **Deploy from GitHub repo**
3. Authorize Railway to access GitHub
4. Select your `smc-trading-bot` repository
5. Click **Deploy Now**

### Enable PostgreSQL:
1. In your Railway project, click **New** (top right)
2. Select **Database** → **Add PostgreSQL**
3. Railway automatically sets `DATABASE_URL` — you don't need to do anything
4. Wait for PostgreSQL to finish provisioning (1–2 minutes)

### Get Railway API Token:
1. Go to railway.app → **Account** → **Tokens**
2. Click **Create Token**
3. Name: `smc-bot-deploy`
4. Copy the token

### Get Service and Project IDs:
1. In your Railway project, click your service (the bot service)
2. Copy the URL — it contains the project and service IDs
3. Or go to Settings → Service → copy the Service ID shown there
4. Project ID is in the project settings

### Add GitHub Actions secret:
1. Go back to GitHub → your repo → Settings → Secrets → Actions
2. Add `RAILWAY_TOKEN` with the Railway token value from above

---

## Step 5 — Set Railway Environment Variables

In Railway dashboard → your project → your service → **Variables** tab.
Click **New Variable** for each one:

```
TELEGRAM_BOT_TOKEN       = (get from Step 7 below)
TELEGRAM_CHAT_ID         = (get from Step 8 below)
ANTHROPIC_API_KEY        = (from https://console.anthropic.com)
TV_WEBHOOK_SECRET        = (any strong password you choose — e.g. smc_webhook_2024_xyz)
ACCOUNT_SIZE             = 100000
RISK_PER_TRADE_PERCENT   = 1.0
MIN_CONFLUENCE_SCORE     = 72
MAX_ACTIVE_TRADES        = 3
MIN_RR_RATIO             = 2.0
PAPER_MODE               = true
GITHUB_TOKEN             = (your GitHub personal access token from Step 2)
GITHUB_USERNAME          = (your GitHub username)
GITHUB_REPO              = smc-trading-bot
RAILWAY_API_TOKEN        = (your Railway token from Step 4)
RAILWAY_SERVICE_ID       = (from Railway dashboard)
RAILWAY_PROJECT_ID       = (from Railway dashboard)
```

DATABASE_URL is set automatically by Railway — do not add it manually.

---

## Step 6 — Get Your Webhook URL

After deployment, Railway gives you a public URL like:
```
https://smc-trading-bot-production.up.railway.app
```

Your TradingView webhook URL will be:
```
https://smc-trading-bot-production.up.railway.app/webhook
```

Save this — you'll need it in Step 12.

Test that your server is running:
```
curl https://your-railway-url.up.railway.app/health
```
Should return: `{"status": "ok", "mode": "paper"}`

---

## Step 7 — Create Telegram Bot

1. Open Telegram (app or web)
2. Search for **@BotFather**
3. Send `/newbot`
4. Choose a name: e.g. `SMC Trading Signals`
5. Choose a username: e.g. `smc_signals_yourname_bot` (must end in `bot`)
6. BotFather sends you a token like: `7123456789:AAF-xxxxxxxxxxxxxxxxxxxxxxxxxxx`
7. Copy this token → add to Railway as `TELEGRAM_BOT_TOKEN`

---

## Step 8 — Get Your Telegram Chat ID

1. Open Telegram
2. Search for **@userinfobot**
3. Send `/start`
4. It replies with your chat ID, e.g. `Your ID: 987654321`
5. Copy this number → add to Railway as `TELEGRAM_CHAT_ID`

---

## Step 9 — Push Code and Confirm Auto-Deploy

```bash
git add .
git commit -m "Add Railway environment variables reference"
git push origin main
```

1. Go to GitHub Actions tab — you should see the deploy workflow running
2. Go to Railway dashboard — you should see a new deployment starting
3. Wait 2–3 minutes for deployment to complete
4. Check Railway **Logs** tab — look for: `SMC Trading Bot starting on port XXXX`

---

## Step 10 — Complete Telegram Onboarding

1. Open Telegram
2. Find your bot (search for the username you chose)
3. Send `/start`
4. The bot should show a market selection keyboard
5. Tap to select your markets (e.g. Indian Stocks, F&O, Indices)
6. Tap **Done → Step 2**
7. Select your trading style
8. Select signal quality filter
9. Tap **Finish Setup** — you are ready to receive signals

---

## Step 11 — Add Pine Script Indicator to TradingView

See [`tradingview_setup.md`](tradingview_setup.md) for detailed instructions.

Quick version:
1. Open TradingView → any chart (e.g. NSE:NIFTY 15m)
2. Click **Pine Editor** at the bottom
3. Paste the entire contents of `tradingview/smc_connector.pine`
4. Click **Add to chart**
5. Confirm you see OBs, BOS/CHOCH labels, session boxes on the chart

---

## Step 12 — Create TradingView Alert with Webhook

1. On your TradingView chart, right-click → **Add Alert**
2. Condition: **SMC Connector** → **Any OB Entry signal**
3. Under **Notifications** → enable **Webhook URL**
4. Paste your webhook URL: `https://your-url.up.railway.app/webhook`
5. In the **Message** field, paste this JSON exactly:

```json
{
  "secret": "YOUR_TV_WEBHOOK_SECRET",
  "instrument": "{{ticker}}",
  "exchange": "{{exchange}}",
  "timeframe": "{{interval}}",
  "signal_type": "OB_ENTRY",
  "direction": "LONG",
  "structure": "CHOCH",
  "ob_high": {{high}},
  "ob_low": {{low}},
  "ob_mid": 0,
  "fvg_present": true,
  "liquidity_swept": true,
  "session": "LONDON",
  "htf_bias": "BULLISH",
  "in_discount": true,
  "trap_present": false,
  "volume_ratio": 1.8,
  "close": {{close}},
  "timestamp": "{{timenow}}"
}
```

Replace `YOUR_TV_WEBHOOK_SECRET` with your actual secret value.

6. Click **Create**
7. Set alert name: `SMC OB Entry - NIFTY 15m` (or similar)
8. Repeat for each instrument and alert type you want

---

## Step 13 — Confirm First Webhook in Railway Logs

1. Go to Railway dashboard → your service → **Logs** tab
2. Trigger a test: On TradingView, manually trigger the alert or wait for a real signal
3. Look for log lines like:
   ```
   Webhook received: NSE:NIFTY 15m OB_ENTRY LONG
   Segment detected: INDICES
   User has INDICES enabled: True
   Score: 84/100 — Grade: A
   Signal sent to Telegram
   ```

---

## Step 14 — Run in Paper Mode for 1–2 Weeks

The system starts in paper mode automatically (`PAPER_MODE=true`).

In paper mode:
- All signals are sent to Telegram as normal
- Trade levels, scores, and confluences are all real
- P&L tracking is simulated (no real money)
- Every signal is marked `📄 Paper Mode`

During this phase:
- Observe signal quality
- Review daily summaries at 3:45 PM IST and 11:30 PM IST
- Check `/performance` after 10+ signals
- Review your first weekly improvement report (Sunday 8 PM IST)

**Recommended: At least 20 paper trades before going live.**

---

## Step 15 — Go Live When Confident

When you are satisfied with paper mode performance:

1. Send `/golive` to your Telegram bot
2. Confirm the switch when prompted
3. The bot updates `PAPER_MODE=false` in the database (not Railway env)
4. All subsequent signals show `🔴 Live Mode`
5. P&L tracking reflects real trade outcomes

---

## Go-Live Checklist

Before sending `/golive`, confirm every item:

- [ ] At least 20 paper trades completed
- [ ] Win rate ≥ 50% in paper mode
- [ ] All 4 API keys are correctly set in Railway
- [ ] TradingView alerts are firing correctly (check Railway logs)
- [ ] First weekly improvement report received and reviewed
- [ ] `/performance` shows expected results
- [ ] `/rollback` tested (shows last 5 deployments)
- [ ] Telegram commands all responding (`/status`, `/markets`, `/active`)
- [ ] Daily summaries arriving at correct times
- [ ] You understand the risk — signals are informational only
- [ ] You have set `ACCOUNT_SIZE` to your actual account size
- [ ] `RISK_PER_TRADE_PERCENT` is set to your actual risk tolerance (1% recommended)

---

## Ongoing Maintenance

**Weekly (automatic):**
- Sunday 8 PM IST: Weekly improvement report arrives on Telegram
- Review and tap `/approve_N` or `/reject_N`

**If something breaks:**
- Send `/rollback` on Telegram
- Select the last working version
- Bot reverts automatically

**To change market selections:**
- Send `/markets`
- Tap the edit buttons

**To pause all signals:**
- Send `/pause`
- Send `/resume` when ready

---

## Troubleshooting

**Bot not responding to /start:**
- Check Railway logs for errors
- Verify `TELEGRAM_BOT_TOKEN` is correct in Railway variables
- Confirm bot is deployed (Railway dashboard shows green)

**Webhook not received:**
- Check TradingView alert is active (clock icon shows)
- Verify webhook URL matches Railway URL exactly
- Check `TV_WEBHOOK_SECRET` matches in both Railway and TradingView alert JSON
- Send test webhook: `curl -X POST your-railway-url/webhook -H "Content-Type: application/json" -d '{"secret":"your_secret","instrument":"NSE:NIFTY","timeframe":"15",...}'`

**Database errors in logs:**
- Confirm Railway PostgreSQL plugin is enabled
- `DATABASE_URL` should appear in Railway Variables tab (auto-set)
- If tables missing, they are created automatically on first startup

**High Railway costs:**
- This bot uses ~$1–3/month normally
- PostgreSQL adds ~$5/month
- Total: ~$6–8/month maximum
