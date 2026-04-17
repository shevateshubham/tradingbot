"""
telegram_interface/command_handler.py — Telegram bot command handlers.

Flow:
  /start or /trade
    → Step 1: Pick market  [NIFTY] [FOREX] [CRYPTO] [GOLD] [ALL]
    → Step 2: Pick type    [⚡ Scalp] [📈 Intraday] [📅 Short Term] [📆 Long Term]
    → Scan runs immediately → ranked list → tap symbol → full signal

Everything is manual. No auto-scan. User controls every scan.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from utils.config_loader import load_config
from utils.logger import get_logger
from utils.market_hours import is_market_open, get_closed_markets_message, get_open_markets
from telegram_interface.session_store import (
    load_session, update_last_run, set_paused, is_paused,
    get_last_market, get_trade_type, set_trade_type,
)

logger = get_logger("telegram")

VALID_MARKETS    = {"NIFTY", "FOREX", "CRYPTO", "GOLD", "ALL"}
VALID_TYPES      = {"scalp", "intraday", "short_term", "long_term"}
TRADES_DIR       = Path(__file__).parent.parent / "data" / "trades"
_SCAN_KEY        = "last_scan_{chat_id}"


# ── Keyboards ──────────────────────────────────────────────────────────────────

def _markets_keyboard(config: dict) -> InlineKeyboardMarkup:
    open_markets = get_open_markets(config)
    buttons, row = [], []
    for market in config.get("markets", []):
        label = f"{'✅' if market in open_markets else '🔒'} {market}"
        row.append(InlineKeyboardButton(label, callback_data=f"market_select:{market}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🌐 ALL Markets", callback_data="market_select:ALL")])
    return InlineKeyboardMarkup(buttons)


def _trade_type_keyboard(market: str) -> InlineKeyboardMarkup:
    types = [
        ("⚡ Scalp",      "scalp",      "1-15 min"),
        ("📈 Intraday",   "intraday",   "Same day"),
        ("📅 Short Term", "short_term", "Days-weeks"),
        ("📆 Long Term",  "long_term",  "Weeks-months"),
    ]
    buttons = [
        [InlineKeyboardButton(f"{label}  ({desc})", callback_data=f"tradetype:{market}:{key}")]
        for label, key, desc in types
    ]
    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="markets")])
    return InlineKeyboardMarkup(buttons)




# ── Pipeline runner ────────────────────────────────────────────────────────────

def _get_trade_type_config(trade_type: str, config: dict) -> dict:
    return config.get("trade_types", {}).get(trade_type, {
        "timeframes": ["1m", "5m", "15m"],
        "htf_tf": "15m", "ltf_tf": "1m",
        "min_rr": 2.0, "scan_interval_minutes": 15,
    })


def _run_pipeline_scoring(market: str, trade_type: str) -> dict:
    """Run Bots 1-5. Returns enriched pipeline dict with decisions per symbol."""
    from bots.bot1_data_collector import DataCollectorBot
    from bots.bot2_context        import ContextBot
    from bots.bot3_setup          import SetupBot
    from bots.bot4_trigger        import TriggerBot
    from bots.bot5_decision       import DecisionBot

    config         = load_config()
    tt_config      = _get_trade_type_config(trade_type, config)
    run_id         = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    pipeline = {
        "run_id":            run_id,
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "selected_market":   market,
        "trade_type":        trade_type,
        "trade_type_config": tt_config,
    }
    pipeline = DataCollectorBot().run(pipeline)
    pipeline = ContextBot().run(pipeline)
    pipeline = SetupBot().run(pipeline)
    pipeline = TriggerBot().run(pipeline)
    pipeline = DecisionBot().run(pipeline)
    return pipeline



# ── Command handlers ───────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = load_config()
    await update.message.reply_text(
        "👋 <b>Trading Pipeline Bot</b>\n\n"
        "Analyzes markets using Smart Money Concepts:\n"
        "Order Blocks · FVGs · Liquidity Sweeps · BOS\n\n"
        "<b>Step 1:</b> Pick a market below\n"
        "<b>Step 2:</b> Pick trade style (Scalp / Intraday / Short Term / Long Term)\n"
        "<b>Step 3:</b> Bot scans → ranked results → tap symbol for full signal\n\n"
        "👇 Choose a market:",
        parse_mode="HTML",
        reply_markup=_markets_keyboard(config),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_command(update, context)


async def markets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = load_config()
    open_m = get_open_markets(config)
    lines  = ["📊 <b>Market Status</b>\n"]
    for market in config.get("markets", []):
        status = "✅ OPEN" if market in open_m else "🔒 CLOSED"
        syms   = config.get("symbols", {}).get(market, [])
        lines.append(f"<b>{market}</b> {status} — {len(syms)} symbols")
    lines.append("\n👇 Select a market:")

    msg = update.message or (update.callback_query and update.callback_query.message)
    await msg.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=_markets_keyboard(config))


async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/trade [MARKET] — show market selection or jump straight to trade type."""
    chat_id = update.effective_chat.id
    config  = load_config()
    args    = context.args or []

    if is_paused(chat_id):
        await update.message.reply_text("⏸ Bot is paused. Send /stop to unpause.")
        return

    if not args:
        await update.message.reply_text(
            "👇 Choose a market:", parse_mode="HTML",
            reply_markup=_markets_keyboard(config),
        )
        return

    market = args[0].upper()
    if market not in VALID_MARKETS:
        await update.message.reply_text(
            f"❌ Unknown market: <b>{market}</b>\nValid: {', '.join(sorted(VALID_MARKETS))}",
            parse_mode="HTML",
        )
        return

    # Market known — ask for trade type
    await update.message.reply_text(
        f"📊 <b>{market}</b> selected.\n👇 Choose trade type:",
        parse_mode="HTML",
        reply_markup=_trade_type_keyboard(market),
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id  = update.effective_chat.id
    session  = load_session(chat_id)
    config   = load_config()

    last_market  = session.get("last_market", "—")
    last_type    = session.get("trade_type", "—")
    last_run     = session.get("last_run", "—")
    run_count    = session.get("run_count", 0)

    today      = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_file = TRADES_DIR / f"{today}.json"
    total_today = wins_today = 0
    if today_file.exists():
        try:
            with open(today_file) as f:
                trades = json.load(f)
            total_today = len(trades)
            wins_today  = sum(1 for t in trades if t.get("outcome") in ("TP1", "TP2", "TP3"))
        except Exception:
            pass

    auto       = config.get("auto_scan", {})
    auto_state = "✅ ON" if auto.get("enabled") else "⏸ OFF"
    auto_mkt   = auto.get("market", "—")
    auto_type  = auto.get("trade_type", "—")
    auto_int   = auto.get("interval_minutes", 15)
    open_str   = ", ".join(get_open_markets(config)) or "None"
    wr_str     = f"{wins_today/total_today*100:.0f}%" if total_today else "—"

    await update.message.reply_text(
        f"📊 <b>Bot Status</b>\n\n"
        f"Auto-scan:  {auto_state}  ({auto_mkt} · {auto_type} · every {auto_int}m)\n"
        f"Last scan:  <b>{last_market}</b> · {last_type}\n"
        f"Last run:   {last_run}\n"
        f"Total scans: {run_count}\n\n"
        f"<b>Today's signals:</b> {total_today} sent | {wins_today} winners | WR: {wr_str}\n\n"
        f"Open markets: {open_str}",
        parse_mode="HTML",
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    paused  = is_paused(chat_id)
    set_paused(chat_id, not paused)
    if paused:
        await update.message.reply_text("▶️ Resumed. Send /trade to scan.", parse_mode="HTML")
    else:
        await update.message.reply_text("⏸ Paused. Send /stop again to resume.")


async def autoscan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kept for weekly review scheduler only — scanning is manual."""
    config = load_config()
    await update.message.reply_text(
        "ℹ️ Scanning is manual — use /start to pick market and trade style.\n\n"
        "Weekly review runs automatically every Sunday 20:00 IST.",
        parse_mode="HTML",
        reply_markup=_markets_keyboard(config),
    )


# ── Callback query (button presses) ───────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    chat_id = update.effective_chat.id
    data    = query.data or ""

    await query.answer()

    # ── Step 1: Market selected → show trade type menu ────────────────────────
    if data.startswith("market_select:"):
        market = data.split(":", 1)[1]
        await query.message.reply_text(
            f"📊 <b>{market}</b> selected.\n👇 Choose trade type:",
            parse_mode="HTML",
            reply_markup=_trade_type_keyboard(market),
        )
        return

    # ── Step 2: Trade type selected → scan → send signals directly ───────────
    if data.startswith("tradetype:"):
        _, market, trade_type = data.split(":", 2)
        config    = load_config()
        tt_config = _get_trade_type_config(trade_type, config)
        label     = tt_config.get("label", trade_type)
        interval  = tt_config.get("scan_interval_minutes", 5)

        set_trade_type(chat_id, trade_type)

        warning = ""
        if market != "ALL" and not is_market_open(market, config):
            warning = f"\n⚠️ {get_closed_markets_message(market, config)}"

        syms        = (config.get("symbols", {}).get(market, []) if market != "ALL"
                       else [s for ss in config.get("symbols", {}).values() for s in ss])
        disabled    = set(config.get("disabled_symbols", []))
        active_syms = [s for s in syms if s not in disabled]

        scanning_msg = await query.message.reply_text(
            f"⏳ Scanning <b>{market}</b> · {label} ({len(active_syms)} symbols)...{warning}",
            parse_mode="HTML",
        )

        try:
            loop   = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, _run_pipeline_scoring, market, trade_type
            )
        except Exception as e:
            logger.error(f"Pipeline error for {market}/{trade_type}: {e}")
            await query.message.reply_text(f"❌ Pipeline error: {e}")
            return

        update_last_run(chat_id, market, trade_type)
        context.application.bot_data[_SCAN_KEY.format(chat_id=chat_id)] = result

        # Send only high-quality TRADE signals (grade A or A+, confidence ≥ 75)
        from bots.bot6_notification import send_signal_for_symbol
        MIN_QUALITY_CONF  = config.get("scoring", {}).get("min_confidence", 70)
        QUALITY_GRADES    = {"A", "A+"}
        trade_count = watch_count = 0
        for sym, sym_data in result.get("symbols", {}).items():
            dec    = sym_data.get("decision", {})
            action = dec.get("action", "NO_TRADE")
            conf   = dec.get("confidence_score", 0)
            grade  = dec.get("grade", "C")
            if action == "TRADE" and grade in QUALITY_GRADES and conf >= MIN_QUALITY_CONF:
                send_signal_for_symbol(sym, sym_data, config, result["run_id"], str(chat_id))
                trade_count += 1
            elif action in ("TRADE", "WATCH"):
                watch_count += 1

        # Summary footer
        rescan_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🔄 Rescan {market} · {label}", callback_data=f"tradetype:{market}:{trade_type}"),
            InlineKeyboardButton("📋 Markets", callback_data="markets"),
        ]])

        if trade_count == 0:
            await query.message.reply_text(
                f"📭 <b>No confident setups</b> in {market} · {label}\n"
                f"<i>{watch_count} symbol(s) approaching — auto-scan will alert you.</i>\n\n"
                f"Auto-scanning every <b>{interval}m</b>.",
                parse_mode="HTML",
                reply_markup=rescan_kb,
            )
        else:
            await query.message.reply_text(
                f"✅ <b>{trade_count} signal(s) sent above</b>\n"
                f"Auto-scanning every <b>{interval}m</b> — next setup will be sent automatically.",
                parse_mode="HTML",
                reply_markup=rescan_kb,
            )

        # Enable background auto-scan with these settings
        from scanner.auto_scanner import start_auto_scanner
        from utils.config_loader import save_config
        auto = config.get("auto_scan", {})
        auto.update({"enabled": True, "market": market, "trade_type": trade_type,
                     "interval_minutes": interval})
        config["auto_scan"] = auto
        save_config(config)
        start_auto_scanner(interval)
        return

    # ── Other ─────────────────────────────────────────────────────────────────
    if data == "markets":
        await markets_command(update, context)

    elif data == "weekly_stats":
        try:
            from weekly_review import get_weekly_summary
            await query.message.reply_text(get_weekly_summary(), parse_mode="HTML")
        except Exception as e:
            await query.message.reply_text(f"Weekly stats unavailable: {e}")

    elif data == "stop":
        await stop_command(update, context)
