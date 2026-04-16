"""
telegram_interface/command_handler.py — Telegram bot command handlers.
Handles /trade, /markets, /status, /stop, /help, /start.
User picks a market → pipeline runs for ONLY that market.
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
    load_session, update_last_run, set_paused, is_paused, get_last_market
)

logger = get_logger("telegram")

VALID_MARKETS = {"NIFTY", "FOREX", "CRYPTO", "GOLD", "ALL"}

TRADES_DIR = Path(__file__).parent.parent / "data" / "trades"


def _rescan_keyboard(market: str) -> InlineKeyboardMarkup:
    """Inline keyboard after a scan result."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Re-scan", callback_data=f"rescan:{market}"),
            InlineKeyboardButton("📊 Weekly Stats", callback_data="weekly_stats"),
        ],
        [
            InlineKeyboardButton("📋 Markets", callback_data="markets"),
            InlineKeyboardButton("⏹ Stop", callback_data="stop"),
        ],
    ])


def _markets_keyboard(config: dict) -> InlineKeyboardMarkup:
    """Inline buttons for each available market."""
    open_markets = get_open_markets(config)
    buttons = []
    row = []
    for market in config.get("markets", []):
        label = f"{'✅' if market in open_markets else '🔒'} {market}"
        row.append(InlineKeyboardButton(label, callback_data=f"trade:{market}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🌐 ALL Markets", callback_data="trade:ALL")])
    return InlineKeyboardMarkup(buttons)


def _run_pipeline(market: str) -> dict:
    """
    Synchronous pipeline orchestrator — runs all 6 bots in sequence.
    Imported here to avoid circular imports. Called in a thread executor.
    """
    # Import here to avoid circular imports at module load time
    from bots.bot1_data_collector import DataCollectorBot
    from bots.bot2_context        import ContextBot
    from bots.bot3_setup          import SetupBot
    from bots.bot4_trigger        import TriggerBot
    from bots.bot5_decision       import DecisionBot
    from bots.bot6_notification   import NotificationBot

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    pipeline = {
        "run_id":          run_id,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "selected_market": market,
    }

    pipeline = DataCollectorBot().run(pipeline)
    pipeline = ContextBot().run(pipeline)
    pipeline = SetupBot().run(pipeline)
    pipeline = TriggerBot().run(pipeline)
    pipeline = DecisionBot().run(pipeline)
    pipeline = NotificationBot().run(pipeline)

    return pipeline


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler: /start"""
    config = load_config()
    keyboard = _markets_keyboard(config)
    await update.message.reply_text(
        "👋 <b>Trading Pipeline Bot</b>\n\n"
        "I analyze markets using Smart Money Concepts (SMC):\n"
        "Order Blocks · FVGs · Liquidity Sweeps · BOS · No retail indicators.\n\n"
        "<b>Commands:</b>\n"
        "/trade &lt;MARKET&gt; — Scan a market (NIFTY, FOREX, CRYPTO, GOLD, ALL)\n"
        "/markets — Show available markets with status\n"
        "/status — Last run + weekly performance\n"
        "/stop — Pause scanning\n"
        "/help — Show this message\n\n"
        "👇 Pick a market to start:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler: /help"""
    await start_command(update, context)


async def markets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler: /markets — show available markets with open/closed status."""
    config = load_config()
    open_m = get_open_markets(config)
    lines  = ["📊 <b>Market Status</b>\n"]
    for market in config.get("markets", []):
        status = "✅ OPEN" if market in open_m else "🔒 CLOSED"
        syms   = config.get("symbols", {}).get(market, [])
        lines.append(f"<b>{market}</b> {status} — {len(syms)} symbols")
    lines.append("\nSend /trade &lt;MARKET&gt; to scan.")
    keyboard = _markets_keyboard(config)
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=keyboard
    )


async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler: /trade <MARKET>"""
    chat_id = update.effective_chat.id
    config  = load_config()

    if is_paused(chat_id):
        await update.message.reply_text(
            "⏸ Bot is paused. Send /stop to unpause, then retry."
        )
        return

    args   = context.args or []
    market = args[0].upper() if args else get_last_market(chat_id) or "CRYPTO"

    if market not in VALID_MARKETS:
        markets_str = ", ".join(VALID_MARKETS)
        await update.message.reply_text(
            f"❌ Unknown market: <b>{market}</b>\n"
            f"Valid options: {markets_str}",
            parse_mode="HTML",
        )
        return

    # Warn if closed but still allow (user explicitly requested)
    warning = ""
    if market != "ALL" and not is_market_open(market, config):
        warning = f"\n⚠️ {get_closed_markets_message(market, config)}"

    syms = config.get("symbols", {}).get(market, []) if market != "ALL" else \
           [s for syms in config.get("symbols", {}).values() for s in syms]
    disabled = set(config.get("disabled_symbols", []))
    active_syms = [s for s in syms if s not in disabled]

    await update.message.reply_text(
        f"⏳ Scanning <b>{market}</b> ({len(active_syms)} symbols)...{warning}\n"
        f"Running: Data → Context → Setup → Trigger → Decision → Notify",
        parse_mode="HTML",
    )

    # Run pipeline in a thread (blocking I/O)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _run_pipeline, market)
    except Exception as e:
        logger.error(f"Pipeline error for {market}: {e}")
        await update.message.reply_text(f"❌ Pipeline error: {e}")
        return

    update_last_run(chat_id, market)

    # Summary message
    summary = result.get("pipeline_summary", {})
    trades  = summary.get("trades", 0)
    watches = summary.get("watches", 0)
    notified_t = summary.get("notifications_sent_trade", 0)
    notified_w = summary.get("notifications_sent_watch", 0)

    if trades == 0 and watches == 0:
        msg = (
            f"✅ <b>Scan complete — {market}</b>\n\n"
            f"No trade setups found right now.\n"
            f"Symbols scanned: {len(active_syms)}\n"
            f"Market session: {_session_label()}"
        )
    else:
        msg = (
            f"✅ <b>Scan complete — {market}</b>\n\n"
            f"🟢 TRADE signals: {notified_t}\n"
            f"👀 WATCH alerts:  {notified_w}\n"
            f"Symbols scanned: {len(active_syms)}"
        )

    await update.message.reply_text(
        msg, parse_mode="HTML", reply_markup=_rescan_keyboard(market)
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler: /status — show last run info and weekly stats."""
    chat_id = update.effective_chat.id
    session = load_session(chat_id)
    config  = load_config()

    last_market = session.get("last_market", "—")
    last_run    = session.get("last_run", "—")
    run_count   = session.get("run_count", 0)

    # Quick trade stats from today's file
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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

    open_markets = get_open_markets(config)
    open_str = ", ".join(open_markets) if open_markets else "None"

    await update.message.reply_text(
        f"📊 <b>Bot Status</b>\n\n"
        f"Last scan: <b>{last_market}</b> @ {last_run}\n"
        f"Total scans: {run_count}\n\n"
        f"<b>Today's trades:</b> {total_today} sent | {wins_today} winners\n"
        f"Win rate: {wins_today/total_today*100:.0f}%" if total_today else
        f"📊 <b>Bot Status</b>\n\nLast scan: <b>{last_market}</b> @ {last_run}\n"
        f"Total scans: {run_count}\n\nNo trades sent today.\n"
        f"Open markets: {open_str}",
        parse_mode="HTML",
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler: /stop — toggle pause."""
    chat_id = update.effective_chat.id
    currently_paused = is_paused(chat_id)
    set_paused(chat_id, not currently_paused)
    if currently_paused:
        await update.message.reply_text("▶️ Resumed. Send /trade &lt;MARKET&gt; to scan.", parse_mode="HTML")
    else:
        await update.message.reply_text("⏸ Paused. Send /stop again to resume.")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler: inline keyboard button presses."""
    query   = update.callback_query
    chat_id = update.effective_chat.id
    data    = query.data or ""

    await query.answer()

    if data.startswith("trade:") or data.startswith("rescan:"):
        market = data.split(":", 1)[1]
        # Simulate /trade command
        context.args = [market]
        await trade_command(update, context)

    elif data == "markets":
        await markets_command(update, context)

    elif data == "weekly_stats":
        from weekly_review import get_weekly_summary
        try:
            summary = get_weekly_summary()
            await query.message.reply_text(summary, parse_mode="HTML")
        except Exception as e:
            await query.message.reply_text(f"Weekly stats unavailable: {e}")

    elif data == "stop":
        await stop_command(update, context)


def _session_label() -> str:
    """UTC session label for display."""
    from utils.market_hours import get_session_label
    return get_session_label()
