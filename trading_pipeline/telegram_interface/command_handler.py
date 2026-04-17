"""
telegram_interface/command_handler.py — Telegram bot command handlers.

Flow:
  /start or /trade
    → Step 1: Pick market  [NIFTY] [FOREX] [CRYPTO] [GOLD] [ALL]
    → Step 2: Pick style   [⚡ Scalp] [📈 Intraday] [📅 Short Term] [📆 Long Term]
    → Continuous scan starts immediately
    → A/A+ TRADE signals sent automatically every N minutes
    → [⏹ Stop Scanning] button shown — market close also stops automatically
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from utils.config_loader import load_config, save_config
from utils.logger import get_logger
from utils.market_hours import is_market_open, get_closed_markets_message, get_open_markets
from telegram_interface.session_store import (
    load_session, update_last_run, set_paused, is_paused,
    get_last_market, get_trade_type, set_trade_type,
)

logger = get_logger("telegram")

VALID_MARKETS = {"NIFTY", "FOREX", "CRYPTO", "GOLD", "ALL"}
TRADES_DIR    = Path(__file__).parent.parent / "data" / "trades"
_SCAN_KEY     = "last_scan_{chat_id}"



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


def _scanning_keyboard(market: str, trade_type: str, label: str, interval: int) -> InlineKeyboardMarkup:
    """Shown while continuous scan is active."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"⏹ Stop Scanning {market}",
            callback_data=f"stop_scan:{market}:{trade_type}",
        )],
        [InlineKeyboardButton("📋 Change Market / Style", callback_data="markets")],
    ])


# ── Pipeline runner ────────────────────────────────────────────────────────────

def _get_trade_type_config(trade_type: str, config: dict) -> dict:
    return config.get("trade_types", {}).get(trade_type, {
        "timeframes": ["1m", "5m", "15m"],
        "htf_tf": "15m", "ltf_tf": "1m",
        "min_rr": 2.0, "scan_interval_minutes": 5,
    })


def _run_pipeline_scoring(market: str, trade_type: str) -> dict:
    from bots.bot1_data_collector import DataCollectorBot
    from bots.bot2_context        import ContextBot
    from bots.bot3_setup          import SetupBot
    from bots.bot4_trigger        import TriggerBot
    from bots.bot5_decision       import DecisionBot

    config    = load_config()
    tt_config = _get_trade_type_config(trade_type, config)
    run_id    = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

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


def _send_quality_signals(result: dict, config: dict, chat_id: str) -> int:
    """Send TRADE signals meeting the confidence threshold. Returns count sent."""
    from bots.bot6_notification import send_signal_for_symbol
    min_conf    = config.get("scoring", {}).get("min_confidence", 65)
    trade_count = 0
    for sym, sym_data in result.get("symbols", {}).items():
        dec    = sym_data.get("decision", {})
        action = dec.get("action", "NO_TRADE")
        conf   = dec.get("confidence_score", 0)
        if action == "TRADE" and conf >= min_conf:
            send_signal_for_symbol(sym, sym_data, config, result["run_id"], chat_id)
            trade_count += 1
    return trade_count


# ── Command handlers ───────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = load_config()
    await update.message.reply_text(
        "👋 <b>Trading Pipeline Bot</b>\n\n"
        "Analyzes markets using Smart Money Concepts:\n"
        "Order Blocks · FVGs · Liquidity Sweeps · BOS\n\n"
        "<b>Step 1:</b> Pick a market\n"
        "<b>Step 2:</b> Pick trade style\n"
        "→ Continuous scan starts — A-grade signals sent automatically\n"
        "→ Stops when market closes or you tap ⏹ Stop\n\n"
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

    await update.message.reply_text(
        f"📊 <b>{market}</b> selected.\n👇 Choose trade style:",
        parse_mode="HTML",
        reply_markup=_trade_type_keyboard(market),
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id  = update.effective_chat.id
    session  = load_session(chat_id)
    config   = load_config()

    last_market = session.get("last_market", "—")
    last_type   = session.get("trade_type", "—")
    last_run    = session.get("last_run", "—")
    run_count   = session.get("run_count", 0)

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
    auto_state = "🔄 SCANNING" if auto.get("enabled") else "⏹ STOPPED"
    auto_mkt   = auto.get("market", "—")
    auto_type  = auto.get("trade_type", "—")
    auto_int   = auto.get("interval_minutes", 5)
    open_str   = ", ".join(get_open_markets(config)) or "None"
    wr_str     = f"{wins_today/total_today*100:.0f}%" if total_today else "—"

    await update.message.reply_text(
        f"📊 <b>Bot Status</b>\n\n"
        f"Scanner:     {auto_state}  ({auto_mkt} · {auto_type} · every {auto_int}m)\n"
        f"Last scan:   <b>{last_market}</b> · {last_type}\n"
        f"Last run:    {last_run}\n"
        f"Total scans: {run_count}\n\n"
        f"<b>Today's signals:</b> {total_today} sent | {wins_today} winners | WR: {wr_str}\n\n"
        f"Open markets: {open_str}",
        parse_mode="HTML",
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop the continuous scanner."""
    from scanner.auto_scanner import set_enabled
    set_enabled(False)
    await update.message.reply_text(
        "⏹ <b>Scanning stopped.</b>\n\nSend /start to pick a new market and style.",
        parse_mode="HTML",
        reply_markup=_markets_keyboard(load_config()),
    )


async def autoscan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = load_config()
    await update.message.reply_text(
        "Use /start to pick a market and trade style — scanning starts automatically.\n"
        "Use /stop to stop the scanner at any time.",
        parse_mode="HTML",
        reply_markup=_markets_keyboard(config),
    )


# ── Callback query (button presses) ───────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    chat_id = update.effective_chat.id
    data    = query.data or ""

    await query.answer()

    # ── Step 1: Market selected → show trade style menu ───────────────────────
    if data.startswith("market_select:"):
        market = data.split(":", 1)[1]
        await query.message.reply_text(
            f"📊 <b>{market}</b> selected.\n👇 Choose trade style:",
            parse_mode="HTML",
            reply_markup=_trade_type_keyboard(market),
        )
        return

    # ── Step 2: Trade style selected → scan → continuous scan starts ──────────
    if data.startswith("tradetype:"):
        _, market, trade_type = data.split(":", 2)
        config    = load_config()
        tt_config = _get_trade_type_config(trade_type, config)
        label     = tt_config.get("label", trade_type)
        interval  = tt_config.get("scan_interval_minutes", 5)

        set_trade_type(chat_id, trade_type)

        # Market closed warning (still scan if user chose — data is data)
        market_closed = market != "ALL" and not is_market_open(market, config)
        warning = f"\n⚠️ {get_closed_markets_message(market, config)}" if market_closed else ""

        syms        = (config.get("symbols", {}).get(market, []) if market != "ALL"
                       else [s for ss in config.get("symbols", {}).values() for s in ss])
        active_syms = [s for s in syms if s not in set(config.get("disabled_symbols", []))]

        await query.message.reply_text(
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

        trade_count = _send_quality_signals(result, config, str(chat_id))
        scan_kb     = _scanning_keyboard(market, trade_type, label, interval)

        if trade_count == 0:
            watching = sum(
                1 for sd in result.get("symbols", {}).values()
                if sd.get("decision", {}).get("action") in ("TRADE", "WATCH")
            )
            await query.message.reply_text(
                f"📭 <b>No A-grade setups found</b> in {market} · {label}\n"
                f"<i>{watching} symbol(s) building — will alert when ready.</i>\n\n"
                f"🔄 Continuous scan active — every <b>{interval}m</b>.\n"
                f"Auto-stops when market closes.",
                parse_mode="HTML",
                reply_markup=scan_kb,
            )
        else:
            await query.message.reply_text(
                f"✅ <b>{trade_count} signal(s) sent above.</b>\n\n"
                f"🔄 Continuous scan active — every <b>{interval}m</b>.\n"
                f"Auto-stops when market closes.",
                parse_mode="HTML",
                reply_markup=scan_kb,
            )

        # Start continuous background scan
        from scanner.auto_scanner import start_auto_scanner
        auto = config.get("auto_scan", {})
        auto.update({
            "enabled":          True,
            "market":           market,
            "trade_type":       trade_type,
            "interval_minutes": interval,
        })
        config["auto_scan"] = auto
        save_config(config)
        start_auto_scanner(interval)
        return

    # ── Stop scanning button ──────────────────────────────────────────────────
    if data.startswith("stop_scan:"):
        from scanner.auto_scanner import set_enabled
        parts      = data.split(":", 2)
        market     = parts[1] if len(parts) > 1 else "—"
        trade_type = parts[2] if len(parts) > 2 else "—"
        set_enabled(False)
        await query.message.reply_text(
            f"⏹ <b>Scanning stopped</b> for {market}.\n\n"
            f"Tap below to pick a new market or style.",
            parse_mode="HTML",
            reply_markup=_markets_keyboard(load_config()),
        )
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
