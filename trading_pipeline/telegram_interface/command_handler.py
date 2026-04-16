"""
telegram_interface/command_handler.py — Telegram bot command handlers.

Flow:
  /start or /trade
    → Step 1: Pick market  [NIFTY] [FOREX] [CRYPTO] [GOLD] [ALL]
    → Step 2: Pick type    [⚡ Scalp] [📈 Intraday] [📅 Short Term] [📆 Long Term]
    → Step 3: Scan options [🔍 Scan Once] [▶️ Auto-Scan every Xm]

  Auto-scan enabled → bot scans every N minutes automatically, sends TRADE signals.
  Scan Once         → ranked list of symbols → tap symbol → full signal.
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
        ("⚡ Scalp",       "scalp",      "1-15 min"),
        ("📈 Intraday",    "intraday",   "Same day"),
        ("📅 Short Term",  "short_term", "Days-weeks"),
        ("📆 Long Term",   "long_term",  "Weeks-months"),
    ]
    buttons = [
        [InlineKeyboardButton(f"{label}  ({desc})", callback_data=f"tradetype:{market}:{key}")]
        for label, key, desc in types
    ]
    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="markets")])
    return InlineKeyboardMarkup(buttons)


def _scan_options_keyboard(market: str, trade_type: str, interval: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Scan Once",               callback_data=f"scan_once:{market}:{trade_type}"),
            InlineKeyboardButton(f"▶️ Auto every {interval}m", callback_data=f"autoscan_start:{market}:{trade_type}"),
        ],
        [InlineKeyboardButton("◀️ Back", callback_data=f"market_select:{market}")],
    ])


def _symbol_selection_keyboard(market: str, trade_type: str, scored: list) -> InlineKeyboardMarkup:
    buttons = []
    for sym, action, conf, grade, _ in scored:
        emoji = "🟢" if action == "TRADE" else ("👀" if action == "WATCH" else "❌")
        buttons.append([InlineKeyboardButton(
            f"{emoji} {sym}  {conf:.0f}%  [{grade}]",
            callback_data=f"signal:{market}:{sym}",
        )])
    buttons.append([
        InlineKeyboardButton("🔄 Re-scan",   callback_data=f"scan_once:{market}:{trade_type}"),
        InlineKeyboardButton("📋 Markets",   callback_data="markets"),
    ])
    return InlineKeyboardMarkup(buttons)


def _after_signal_keyboard(market: str, trade_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Re-scan",      callback_data=f"scan_once:{market}:{trade_type}"),
            InlineKeyboardButton("📋 Markets",      callback_data="markets"),
        ],
        [
            InlineKeyboardButton("📊 Weekly Stats", callback_data="weekly_stats"),
            InlineKeyboardButton("⏹ Stop Auto",     callback_data="stop_autoscan"),
        ],
    ])


def _autoscan_active_keyboard(market: str, trade_type: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Scan Now",    callback_data=f"scan_once:{market}:{trade_type}")],
        [InlineKeyboardButton("⏹ Stop Auto-Scan", callback_data="stop_autoscan")],
        [InlineKeyboardButton("📋 Change Market",  callback_data="markets")],
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
        "<b>Step 1:</b> Pick a market\n"
        "<b>Step 2:</b> Pick trade type (Scalp / Intraday / Short / Long)\n"
        "<b>Step 3:</b> Scan once <i>or</i> start auto-scan\n\n"
        "Auto-scan runs continuously and sends TRADE signals automatically — "
        "so you never miss a setup.\n\n"
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
    from scanner.auto_scanner import get_status, set_enabled, update_interval

    args   = [a.lower() for a in (context.args or [])]
    config = load_config()

    if args:
        arg = args[0]
        if arg == "on":
            set_enabled(True)
            auto     = load_config().get("auto_scan", {})
            interval = auto.get("interval_minutes", 15)
            market   = auto.get("market", "CRYPTO")
            tt       = auto.get("trade_type", "intraday")
            await update.message.reply_text(
                f"✅ <b>Auto-scan ENABLED</b>\n"
                f"Market: <b>{market}</b> · Type: <b>{tt}</b> · every <b>{interval}m</b>\n\n"
                f"Use /autoscan off to disable.",
                parse_mode="HTML",
            )
            return
        if arg == "off":
            set_enabled(False)
            await update.message.reply_text("⏸ <b>Auto-scan DISABLED</b>", parse_mode="HTML")
            return
        if arg.endswith("m") and arg[:-1].isdigit():
            minutes = int(arg[:-1])
            if not 1 <= minutes <= 1440:
                await update.message.reply_text("❌ Interval must be 1m–1440m.")
                return
            update_interval(minutes, config)
            await update.message.reply_text(
                f"⏱ <b>Interval updated to {minutes} minutes</b>", parse_mode="HTML"
            )
            return

    status      = get_status()
    enabled_str = "✅ ENABLED" if status["enabled"] else "⏸ DISABLED"
    next_str    = status["next_run"] or "—"
    auto        = config.get("auto_scan", {})
    market      = auto.get("market", "—")
    tt          = auto.get("trade_type", "—")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⏹ Disable" if status["enabled"] else "▶️ Enable",
                callback_data="autoscan:toggle",
            ),
            InlineKeyboardButton("🔄 Scan Now", callback_data="autoscan:now"),
        ],
        [
            InlineKeyboardButton("5m",  callback_data="autoscan:interval:5"),
            InlineKeyboardButton("15m", callback_data="autoscan:interval:15"),
            InlineKeyboardButton("30m", callback_data="autoscan:interval:30"),
            InlineKeyboardButton("1h",  callback_data="autoscan:interval:60"),
        ],
        [InlineKeyboardButton("📋 Change Market/Type", callback_data="markets")],
    ])

    await update.message.reply_text(
        f"🤖 <b>Auto-Scanner Status</b>\n\n"
        f"State:     {enabled_str}\n"
        f"Market:    {market}\n"
        f"Type:      {tt}\n"
        f"Interval:  every {status['interval_minutes']} minutes\n"
        f"Next run:  {next_str}\n\n"
        f"/autoscan on · off · 5m · 15m · 30m · 1h",
        parse_mode="HTML",
        reply_markup=keyboard,
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

    # ── Step 2: Trade type selected → show scan options ───────────────────────
    if data.startswith("tradetype:"):
        _, market, trade_type = data.split(":", 2)
        config    = load_config()
        tt_config = _get_trade_type_config(trade_type, config)
        interval  = tt_config.get("scan_interval_minutes", 15)
        label     = tt_config.get("label", trade_type)
        desc      = tt_config.get("description", "")

        set_trade_type(chat_id, trade_type)

        await query.message.reply_text(
            f"✅ <b>{market}</b> · {label}\n"
            f"<i>{desc}</i>\n\n"
            f"Timeframes: {' → '.join(tt_config.get('timeframes', []))}\n"
            f"Min R:R: {tt_config.get('min_rr', 2.0)}\n\n"
            f"Auto-scan fires every <b>{interval}m</b> and sends signals automatically.\n"
            f"👇 How do you want to proceed?",
            parse_mode="HTML",
            reply_markup=_scan_options_keyboard(market, trade_type, interval),
        )
        return

    # ── Step 3a: Scan once ────────────────────────────────────────────────────
    if data.startswith("scan_once:"):
        _, market, trade_type = data.split(":", 2)
        config    = load_config()
        tt_config = _get_trade_type_config(trade_type, config)

        warning = ""
        if market != "ALL" and not is_market_open(market, config):
            warning = f"\n⚠️ {get_closed_markets_message(market, config)}"

        syms        = (config.get("symbols", {}).get(market, []) if market != "ALL"
                       else [s for ss in config.get("symbols", {}).values() for s in ss])
        disabled    = set(config.get("disabled_symbols", []))
        active_syms = [s for s in syms if s not in disabled]
        label       = tt_config.get("label", trade_type)

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

    # ── Step 3b: Start auto-scan ──────────────────────────────────────────────
    if data.startswith("autoscan_start:"):
        _, market, trade_type = data.split(":", 2)
        from scanner.auto_scanner import start_auto_scanner

        config    = load_config()
        tt_config = _get_trade_type_config(trade_type, config)
        interval  = tt_config.get("scan_interval_minutes", 15)
        label     = tt_config.get("label", trade_type)

        # Persist preferences to config
        auto = config.get("auto_scan", {})
        auto["enabled"]          = True
        auto["market"]           = market
        auto["trade_type"]       = trade_type
        auto["interval_minutes"] = interval
        config["auto_scan"]      = auto
        save_config(config)

        start_auto_scanner(interval)
        update_last_run(chat_id, market, trade_type)

        await query.message.reply_text(
            f"✅ <b>Auto-scan started!</b>\n\n"
            f"Market: <b>{market}</b>\n"
            f"Type: {label}\n"
            f"Interval: every <b>{interval} minutes</b>\n\n"
            f"TRADE signals will be sent automatically when setups are found.\n"
            f"Running first scan now...",
            parse_mode="HTML",
            reply_markup=_autoscan_active_keyboard(market, trade_type),
        )

        # Run first scan immediately in background
        try:
            loop   = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, _run_pipeline_scoring, market, trade_type
            )
            ranked  = _extract_ranked_symbols(result)
            summary = _build_scan_summary(market, trade_type, ranked, result)
            context.application.bot_data[_SCAN_KEY.format(chat_id=chat_id)] = result
            await query.message.reply_text(
                summary, parse_mode="HTML",
                reply_markup=_symbol_selection_keyboard(market, trade_type, ranked),
            )
        except Exception as e:
            logger.error(f"Initial auto-scan error: {e}")
            await query.message.reply_text(f"⚠️ First scan failed: {e}. Auto-scan still active.")
        return

    # ── Stop auto-scan ────────────────────────────────────────────────────────
    if data == "stop_autoscan":
        from scanner.auto_scanner import set_enabled
        set_enabled(False)
        await query.message.reply_text(
            "⏹ Auto-scan stopped.\n\nSend /start to pick a new market and trade type.",
            parse_mode="HTML",
        )
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

    # ── Auto-scan toggle/now/interval (legacy /autoscan buttons) ─────────────
    if data.startswith("autoscan:"):
        from scanner.auto_scanner import get_status, set_enabled, update_interval, run_auto_scan
        sub = data[len("autoscan:"):]

        if sub == "toggle":
            status    = get_status()
            new_state = not status["enabled"]
            set_enabled(new_state)
            label = "ENABLED ✅" if new_state else "DISABLED ⏸"
            await query.message.reply_text(f"Auto-scan {label}", parse_mode="HTML")

        elif sub == "now":
            await query.message.reply_text("⏳ Running auto-scan now...")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_auto_scan)
            await query.message.reply_text("✅ Auto-scan complete.")

        elif sub.startswith("interval:"):
            minutes = int(sub.split(":")[1])
            config  = load_config()
            update_interval(minutes, config)
            await query.message.reply_text(
                f"⏱ Interval set to <b>{minutes} minutes</b>.", parse_mode="HTML"
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
