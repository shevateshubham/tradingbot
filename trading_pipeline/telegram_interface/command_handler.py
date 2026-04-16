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


def _symbol_selection_keyboard(market: str, trade_type: str, scored: list) -> InlineKeyboardMarkup:
    buttons = []
    for sym, action, conf, grade, _ in scored:
        emoji = "🟢" if action == "TRADE" else ("👀" if action == "WATCH" else "❌")
        buttons.append([InlineKeyboardButton(
            f"{emoji} {sym}  {conf:.0f}%  [{grade}]",
            callback_data=f"signal:{market}:{sym}",
        )])
    buttons.append([
        InlineKeyboardButton("🔄 Re-scan",  callback_data=f"tradetype:{market}:{trade_type}"),
        InlineKeyboardButton("📋 Markets",  callback_data="markets"),
    ])
    return InlineKeyboardMarkup(buttons)


def _after_signal_keyboard(market: str, trade_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Re-scan",      callback_data=f"tradetype:{market}:{trade_type}"),
            InlineKeyboardButton("📋 Markets",      callback_data="markets"),
        ],
        [InlineKeyboardButton("📊 Weekly Stats",    callback_data="weekly_stats")],
    ])


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


def _extract_ranked_symbols(pipeline: dict) -> list[tuple]:
    ranked = []
    for sym, sym_data in pipeline.get("symbols", {}).items():
        if sym_data.get("fetch_status") != "ok":
            continue
        dec    = sym_data.get("decision", {})
        action = dec.get("action", "NO_TRADE")
        conf   = dec.get("confidence_score", 0.0)
        grade  = dec.get("grade", "C")
        setup  = sym_data.get("setup", {}).get("setup_type", "—")
        ranked.append((sym, action, conf, grade, setup))
    ranked.sort(key=lambda x: x[2], reverse=True)
    return ranked


def _build_scan_summary(market: str, trade_type: str, ranked: list, pipeline: dict) -> str:
    config    = load_config()
    tt_label  = config.get("trade_types", {}).get(trade_type, {}).get("label", trade_type)
    timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines     = [f"📊 <b>{market} · {tt_label} — {timestamp}</b>\n"]

    if not ranked:
        lines.append("No data fetched. Market may be closed or API unavailable.")
        return "\n".join(lines)

    for sym, action, conf, grade, setup_type in ranked:
        sym_data  = pipeline["symbols"].get(sym, {})
        htf_bias  = sym_data.get("context", {}).get("htf_bias", "?")
        direction = sym_data.get("setup", {}).get("direction", "?")
        swept     = sym_data.get("setup", {}).get("liquidity_swept", False)
        bos       = sym_data.get("trigger", {}).get("ltf_bos", False)
        trap      = sym_data.get("setup", {}).get("trap_detected", False)

        status = "🟢 TRADE" if action == "TRADE" else ("👀 WATCH" if action == "WATCH" else "❌ SKIP")
        tags   = (["Sweep✓"] if swept else []) + (["BOS✓"] if bos else []) + (["⚠️Trap"] if trap else [])
        tag_str = "  " + " ".join(tags) if tags else ""

        lines.append(
            f"<b>{sym}</b>  {status}  {conf:.0f}% [{grade}]\n"
            f"  HTF: {htf_bias} | Dir: {direction} | {setup_type}{tag_str}"
        )

    actionable = sum(1 for _, a, _, _, _ in ranked if a in ("TRADE", "WATCH"))
    lines.append(
        f"\n<i>{actionable} actionable setup{'s' if actionable != 1 else ''} found.</i>\n"
        f"👇 Tap a symbol for the full trade signal:"
    )
    return "\n".join(lines)


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

    # ── Step 2: Trade type selected → scan immediately ────────────────────────
    if data.startswith("tradetype:"):
        _, market, trade_type = data.split(":", 2)
        config    = load_config()
        tt_config = _get_trade_type_config(trade_type, config)
        label     = tt_config.get("label", trade_type)

        set_trade_type(chat_id, trade_type)

        warning = ""
        if market != "ALL" and not is_market_open(market, config):
            warning = f"\n⚠️ {get_closed_markets_message(market, config)}"

        syms        = (config.get("symbols", {}).get(market, []) if market != "ALL"
                       else [s for ss in config.get("symbols", {}).values() for s in ss])
        disabled    = set(config.get("disabled_symbols", []))
        active_syms = [s for s in syms if s not in disabled]

        await query.message.reply_text(
            f"⏳ Scanning <b>{market}</b> · {label} ({len(active_syms)} symbols)...{warning}\n"
            f"Timeframes: {' → '.join(tt_config.get('timeframes', []))}",
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

        ranked   = _extract_ranked_symbols(result)
        summary  = _build_scan_summary(market, trade_type, ranked, result)
        keyboard = _symbol_selection_keyboard(market, trade_type, ranked)

        await query.message.reply_text(summary, parse_mode="HTML", reply_markup=keyboard)
        return

    # ── Symbol selected → send full signal ───────────────────────────────────
    if data.startswith("signal:"):
        _, market, sym = data.split(":", 2)
        key    = _SCAN_KEY.format(chat_id=chat_id)
        result = context.application.bot_data.get(key)
        trade_type = get_trade_type(chat_id) or "intraday"

        if not result:
            await query.message.reply_text(
                f"⚠️ Scan data expired. Please re-scan.",
                reply_markup=_trade_type_keyboard(market),
            )
            return

        sym_data = result.get("symbols", {}).get(sym)
        if not sym_data:
            await query.message.reply_text(f"⚠️ No data for {sym}. Try re-scanning.")
            return

        from bots.bot6_notification import send_signal_for_symbol
        config = load_config()
        run_id = result.get("run_id", "unknown")

        msg = send_signal_for_symbol(
            sym            = sym,
            sym_data       = sym_data,
            config         = config,
            run_id         = run_id,
            target_chat_id = str(chat_id),
        )
        await query.message.reply_text(
            msg or f"Signal for {sym} sent ☝️",
            parse_mode="HTML",
            reply_markup=_after_signal_keyboard(market, trade_type),
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
