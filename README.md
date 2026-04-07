# SMC Trading Bot — Institutional-Grade Smart Money Signal System

Zero indicators. Pure price action. Fully automated signal delivery via Telegram.

```
╔══════════════════════════════════════════════════════════════════════════╗
║              SMC TRADING BOT — SYSTEM ARCHITECTURE                      ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                          ║
║  [TradingView Chart]                                                     ║
║    └── Pine Script v5 (pure OHLCV — zero indicators)                    ║
║          └── Detects: OB, FVG, BOS, CHOCH, Liquidity, Traps            ║
║                └── Fires webhook → Railway.app                           ║
║                                                                          ║
║  [Railway.app — 24/7 Server]                                             ║
║    ├── FastAPI webhook receiver (validates TV_WEBHOOK_SECRET)            ║
║    ├── Segment detector (ticker → market category)                       ║
║    ├── Signal filter (user preference gating)                            ║
║    ├── SMC processor (enriches payload)                                  ║
║    ├── NSE Options API (Max Pain, PCR, GEX — indices/FNO only)          ║
║    ├── Trade scorer (confluence 0-100, hard discard rules)               ║
║    ├── Trade calculator (entry, SL, TP1-3, lots, charges)               ║
║    ├── Telegram bot (signals, commands, onboarding)                      ║
║    ├── Price tracker (SL/TP monitoring every 5 min)                      ║
║    ├── Weekly self-improvement engine (Claude API + GitHub PR)           ║
║    └── PostgreSQL database (signals, users, performance)                 ║
║                                                                          ║
║  [Telegram Bot]                                                           ║
║    ├── /start → 3-step market selection onboarding                       ║
║    ├── Signal messages (formatted, levels, confluences)                  ║
║    ├── Outcome notifications (SL/TP hit alerts)                          ║
║    ├── Daily session reports (3:45 PM + 11:30 PM IST)                   ║
║    ├── Weekly improvement reports + /approve /reject                     ║
║    └── /rollback → instant version restore                               ║
║                                                                          ║
║  [GitHub Repository]                                                      ║
║    ├── main branch — protected, always working                           ║
║    ├── changes branch — AI suggestions go here                           ║
║    ├── GitHub Actions — test.yml + deploy.yml + backup.yml               ║
║    └── PR required before any merge to main                              ║
║                                                                          ║
║  [Claude API]                                                             ║
║    └── Weekly analysis → single parameter suggestion → GitHub PR         ║
║                                                                          ║
╚══════════════════════════════════════════════════════════════════════════╝
```

## Signal Flow

```
TradingView Alert
      │
      ▼
POST /webhook  ──► Validate secret
      │
      ▼
Segment Detection  ──► NSE:NIFTY* → INDICES
      │
      ▼
Signal Filter  ──► User has INDICES enabled? Timeframe match?
      │
      ▼
SMC Processor  ──► Enrich payload, fetch options data
      │
      ▼
Trade Scorer  ──► Confluence score 0-100, grade A+/A/B/C
      │
      ▼
Trade Calculator  ──► Entry, SL, TP1/2/3, lots, charges
      │
      ▼
Telegram Signal  ──► Formatted message with all levels
      │
      ▼
Price Tracker  ──► Monitor → outcome notification on SL/TP
```

## Requirements

- **No broker API** — signals and chart marking only
- **No indicators** — pure OHLCV price structure math
- **No laptop needed** — Railway runs 24/7
- **4 API keys only**: Telegram bot, Telegram chat ID, Anthropic, TV webhook secret

## Quick Start

See [`docs/setup_guide.md`](docs/setup_guide.md) for complete step-by-step instructions.

## Market Coverage

| Segment | Examples | Options Data |
|---------|----------|-------------|
| Indian Stocks | NSE top 200 equities | No |
| Indian F&O | Top 50 F&O stocks | Per-stock OI |
| Indices | Nifty 50, Bank Nifty, Fin Nifty | Full chain |
| Commodity MCX | Gold, Silver, Crude, NG | No |
| Currency/Forex | USDINR, EURUSD, etc. | No |
| Crypto | BTC, ETH, top 15 by volume | No |
| Global Indices | SPX, NASDAQ, DAX, etc. | No |

## Score Grades

| Grade | Score | Action |
|-------|-------|--------|
| A+ | ≥ 90 | 🚨 Send immediately |
| A | 72–89 | ✅ Send normally |
| B | 55–71 | 📋 Log only, never send |
| C | < 55 | 🗑 Silent discard |

## Environment Variables

Set all in Railway dashboard — never in code or `.env` file:

```
TELEGRAM_BOT_TOKEN      ← from @BotFather
TELEGRAM_CHAT_ID        ← your Telegram chat ID
ANTHROPIC_API_KEY       ← from console.anthropic.com
TV_WEBHOOK_SECRET       ← any strong password you choose
DATABASE_URL            ← auto-provided by Railway PostgreSQL
ACCOUNT_SIZE            ← e.g. 100000
RISK_PER_TRADE_PERCENT  ← e.g. 1.0
MIN_CONFLUENCE_SCORE    ← default 72
MAX_ACTIVE_TRADES       ← default 3
MIN_RR_RATIO            ← default 2.0
PAPER_MODE              ← true (start here)
```

## License

Private use only. Not for redistribution.
