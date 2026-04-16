"""
telegram_interface/command_handler.py — Telegram bot command handlers.

Flow:
  /trade CRYPTO
    → scans all CRYPTO symbols (Bots 1-5, NO auto-send)
    → shows ranked list of symbols with inline buttons
  User taps [🟢 BTC-USD 79% A]
    → sends full trade signal for that symbol only (Bot 6)

This way the user chooses which setup to act on.
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
    load_session, update_last_run, set_paused, is_paused, get_last_market,
)

logger = get_logger("telegram")

VALID_MARKETS = {"NIFTY", "FOREX", "CRYPTO", "GOLD", "ALL"}
TRADES_DIR    = Path(__file__).parent.parent / "data" / "trades"

# bot_data key for storing last scan result per chat
_SCAN_KEY = "last_scan_{chat_id}"


# ── Keyboards ──────────────────────────────────────────────────────────────────

def _markets_keyboard(config: dict) -> InlineKeyboardMarkup:
    open_markets = get_open_markets(config)
    buttons, row = [], []
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


def _symbol_selection_keyboard(market: str, scored_symbols: list[tuple]) -> InlineKeyboardMarkup:
    """
    Build an inline keyboard with one button per symbol, ranked by confidence.
    scored_symbols: list of (sym, action, confidence, grade, setup_type)
    """
    buttons = []
    for sym, action, conf, grade, setup_type in scored_symbols:
        if action == "TRADE":
            emoji = "🟢"
        elif action == "WATCH":
            emoji = "👀"
        else:
            emoji = "❌"
        # callback_data max 64 bytes — sym + market well within limit
        label = f"{emoji} {sym}  {conf:.0f}%  [{grade}]"
        buttons.append([InlineKeyboardButton(label, callback_data=f"signal:{market}:{sym}")])

    # Footer controls
    buttons.append([
        InlineKeyboardButton("🔄 Re-scan", callback_data=f"rescan:{market}"),
        InlineKeyboardButton("📋 Markets", callback_data="markets"),
    ])
    return InlineKeyboardMarkup(buttons)


def _after_signal_keyboard(market: str) -> InlineKeyboardMarkup:
    """Keyboard shown after a signal detail is sent."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Re-scan", callback_data=f"rescan:{market}"),
            InlineKeyboardButton("📋 Markets", callback_data="markets"),
        ],
        [
            InlineKeyboardButton("📊 Weekly Stats", callback_data="weekly_stats"),
            InlineKeyboardButton("⏹ Stop",          callback_data="stop"),
        ],
    ])


# ── Pipeline runner (Bots 1-5 only, no auto-notification) ─────────────────────

def _run_pipeline_scoring(market: str) -> dict:
    """
    Run Bots 1-5 only. Returns enriched pipeline dict with decisions per symbol.
    Bot 6 (notification) is NOT called here — the user selects which signal to send.
    """
    from bots.bot1_data_collector import DataCollectorBot
    from bots.bot2_context        import ContextBot
    from bots.bot3_setup          import SetupBot
    from bots.bot4_trigger        import TriggerBot
    from bots.bot5_decision       import DecisionBot

    run_id   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
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
    return pipeline


def _extract_ranked_symbols(pipeline: dict) -> list[tuple]:
    """
    Extract symbols with their scores from the pipeline result.
    Returns list of (sym, action, confidence, grade, setup_type) sorted by confidence desc.
    """
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


def _build_scan_summary(market: str, ranked: list[tuple], pipeline: dict) -> str:
    """Build the scan results summary message."""
    timestamp = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [f"📊 <b>{market} Scan — {timestamp}</b>\n"]

    if not ranked:
        lines.append("No data fetched. Market may be closed or API unavailable.")
        return "\n".join(lines)

    for sym, action, conf, grade, setup_type in ranked:
        sym_data   = pipeline["symbols"].get(sym, {})
        htf_bias   = sym_data.get("context", {}).get("htf_bias", "?")
        direction  = sym_data.get("setup", {}).get("direction", "?")
        swept      = sym_data.get("setup", {}).get("liquidity_swept", False)
        bos        = sym_data.get("trigger", {}).get("ltf_bos", False)
        trap       = sym_data.get("setup", {}).get("trap_detected", False)

        if action == "TRADE":
            status = "🟢 TRADE"
        elif action == "WATCH":
            status = "👀 WATCH"
        else:
            status = "❌ SKIP"

        tags = []
        if swept: tags.append("Sweep✓")
        if bos:   tags.append("BOS✓")
        if trap:  tags.append("⚠️Trap")

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
    config   = load_config()
    keyboard = _markets_keyboard(config)
    await update.message.reply_text(
        "👋 <b>Trading Pipeline Bot</b>\n\n"
        "Analyzes markets using Smart Money Concepts (SMC):\n"
        "Order Blocks · FVGs · Liquidity Sweeps · BOS · No retail indicators.\n\n"
        "<b>How it works:</b>\n"
        "1️⃣ Pick a market below (or /trade CRYPTO)\n"
        "2️⃣ Bot scans all symbols, ranks setups by confidence\n"
        "3️⃣ Tap any symbol to get the full trade signal\n\n"
        "<b>Commands:</b>\n"
        "/trade &lt;MARKET&gt; — Manual scan (NIFTY · FOREX · CRYPTO · GOLD · ALL)\n"
        "/autoscan       — Auto-scan settings (enable/disable/interval)\n"
        "/markets        — Market open/closed status\n"
        "/status         — Today's performance\n"
        "/stop           — Pause/resume\n\n"
        "💡 <b>Tip:</b> Use /autoscan on to get signals automatically!\n\n"
        "👇 Pick a market for a manual scan:",
        parse_mode="HTML",
        reply_markup=keyboard,
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
    lines.append("\n👇 Select a market to scan:")
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=_markets_keyboard(config),
    )


async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /trade <MARKET>
    Step 1: scan all symbols → show ranked selection list.
    Step 2 (via button_callback): user taps a symbol → full signal sent.
    """
    # Determine if called from a message or a button callback
    if update.callback_query:
        send = update.callback_query.message.reply_text
        chat_id = update.callback_query.message.chat_id
    else:
        send = update.message.reply_text
        chat_id = update.message.chat_id

    config = load_config()

    if is_paused(chat_id):
        await send("⏸ Bot is paused. Send /stop to unpause.")
        return

    args   = context.args or []
    market = args[0].upper() if args else get_last_market(chat_id) or "CRYPTO"

    if market not in VALID_MARKETS:
        await send(
            f"❌ Unknown market: <b>{market}</b>\n"
            f"Valid: {', '.join(sorted(VALID_MARKETS))}",
            parse_mode="HTML",
        )
        return

    # Warn if closed (but scan anyway — user explicitly asked)
    warning = ""
    if market != "ALL" and not is_market_open(market, config):
        warning = f"\n⚠️ {get_closed_markets_message(market, config)}"

    syms = (
        config.get("symbols", {}).get(market, []) if market != "ALL"
        else [s for ss in config.get("symbols", {}).values() for s in ss]
    )
    disabled    = set(config.get("disabled_symbols", []))
    active_syms = [s for s in syms if s not in disabled]

    scanning_msg = await send(
        f"⏳ Scanning <b>{market}</b> ({len(active_syms)} symbols)...{warning}\n"
        f"Pipeline: Data → Context → Setup → Trigger → Decision",
        parse_mode="HTML",
    )

    # Run Bots 1-5 in a thread (blocking I/O must not block event loop)
    try:
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _run_pipeline_scoring, market)
    except Exception as e:
        logger.error(f"Pipeline error for {market}: {e}")
        await send(f"❌ Pipeline error: {e}")
        return

    update_last_run(chat_id, market)

    # Store scan result in bot_data so button_callback can retrieve it
    key = _SCAN_KEY.format(chat_id=chat_id)
    context.application.bot_data[key] = result

    # Build ranked list and summary
    ranked  = _extract_ranked_symbols(result)
    summary = _build_scan_summary(market, ranked, result)
    keyboard = _symbol_selection_keyboard(market, ranked)

    await send(summary, parse_mode="HTML", reply_markup=keyboard)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id  = update.effective_chat.id
    session  = load_session(chat_id)
    config   = load_config()

    last_market = session.get("last_market", "—")
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

    open_str = ", ".join(get_open_markets(config)) or "None"
    wr_str   = f"{wins_today/total_today*100:.0f}%" if total_today else "—"

    await update.message.reply_text(
        f"📊 <b>Bot Status</b>\n\n"
        f"Last scan: <b>{last_market}</b>\n"
        f"Last run:  {last_run}\n"
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
        await update.message.reply_text("▶️ Resumed. Send /trade &lt;MARKET&gt; to scan.", parse_mode="HTML")
    else:
        await update.message.reply_text("⏸ Paused. Send /stop again to resume.")


# ── Callback query (button presses) ───────────────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    chat_id = update.effective_chat.id
    data    = query.data or ""

    await query.answer()

    # ── Market scan / rescan ──────────────────────────────────────────────────
    if data.startswith("trade:") or data.startswith("rescan:"):
        market       = data.split(":", 1)[1]
        context.args = [market]
        await trade_command(update, context)
        return

    # ── Symbol selected → send full signal ───────────────────────────────────
    if data.startswith("signal:"):
        _, market, sym = data.split(":", 2)
        key    = _SCAN_KEY.format(chat_id=chat_id)
        result = context.application.bot_data.get(key)

        if not result:
            await query.message.reply_text(
                "⚠️ Scan data expired. Please re-scan with /trade " + market
            )
            return

        sym_data = result.get("symbols", {}).get(sym)
        if not sym_data:
            await query.message.reply_text(f"⚠️ No data for {sym}. Try re-scanning.")
            return

        # Send the full signal via Bot 6 (on-demand, targeted to this chat)
        from bots.bot6_notification import send_signal_for_symbol
        config = load_config()
        run_id = result.get("run_id", "unknown")

        msg = send_signal_for_symbol(
            sym       = sym,
            sym_data  = sym_data,
            config    = config,
            run_id    = run_id,
            target_chat_id = str(chat_id),
        )

        # Show follow-up controls
        await query.message.reply_text(
            msg or f"Signal for {sym} sent above ☝️",
            parse_mode="HTML",
            reply_markup=_after_signal_keyboard(market),
        )
        return

    # ── Auto-scan controls ────────────────────────────────────────────────────
    if data.startswith("autoscan:"):
        from scanner.auto_scanner import (
            get_status, set_enabled, update_interval, run_auto_scan,
        )
        sub = data[len("autoscan:"):]

        if sub == "toggle":
            status = get_status()
            new_state = not status["enabled"]
            set_enabled(new_state)
            label = "ENABLED ✅" if new_state else "DISABLED ⏸"
            await query.message.reply_text(
                f"Auto-scan {label}\nUse /autoscan to manage settings.",
                parse_mode="HTML",
            )

        elif sub == "now":
            await query.message.reply_text("⏳ Running auto-scan now...")
            config = load_config()
            loop   = asyncio.get_event_loop()
            await loop.run_in_executor(None, run_auto_scan)
            await query.message.reply_text("✅ Auto-scan complete — check for signals above.")

        elif sub.startswith("interval:"):
            minutes = int(sub.split(":")[1])
            config  = load_config()
            update_interval(minutes, config)
            await query.message.reply_text(
                f"⏱ Interval set to <b>{minutes} minutes</b>.", parse_mode="HTML"
            )

        elif sub == "watches":
            config = load_config()
            auto   = config.get("auto_scan", {})
            auto["send_watches"] = not auto.get("send_watches", False)
            config["auto_scan"]  = auto
            save_config(config)
            state = "ON" if auto["send_watches"] else "OFF"
            await query.message.reply_text(
                f"👀 WATCH alerts: <b>{state}</b>", parse_mode="HTML"
            )
        return

    # ── Other buttons ─────────────────────────────────────────────────────────
    if data == "markets":
        await markets_command(update, context)

    elif data == "weekly_stats":
        try:
            from weekly_review import get_weekly_summary
            summary = get_weekly_summary()
            await query.message.reply_text(summary, parse_mode="HTML")
        except Exception as e:
            await query.message.reply_text(f"Weekly stats unavailable: {e}")

    elif data == "stop":
        await stop_command(update, context)


async def autoscan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /autoscan              — show current status
    /autoscan on           — enable auto-scan
    /autoscan off          — disable auto-scan
    /autoscan 5m / 15m / 30m — change interval
    /autoscan watches on/off  — toggle WATCH alerts
    """
    from scanner.auto_scanner import get_status, set_enabled, update_interval

    args    = [a.lower() for a in (context.args or [])]
    config  = load_config()

    # Handle sub-commands
    if args:
        arg = args[0]

        if arg == "on":
            set_enabled(True)
            interval = config.get("auto_scan", {}).get("interval_minutes", 15)
            await update.message.reply_text(
                f"✅ <b>Auto-scan ENABLED</b>\n"
                f"Scanning every <b>{interval} min</b> — signals sent automatically.\n\n"
                f"Use /autoscan off to disable.",
                parse_mode="HTML",
            )
            return

        if arg == "off":
            set_enabled(False)
            await update.message.reply_text(
                "⏸ <b>Auto-scan DISABLED</b>\nUse /autoscan on to re-enable.",
                parse_mode="HTML",
            )
            return

        # Interval: 5m / 15m / 30m / 60m
        if arg.endswith("m") and arg[:-1].isdigit():
            minutes = int(arg[:-1])
            if not 1 <= minutes <= 1440:
                await update.message.reply_text("❌ Interval must be between 1m and 1440m.")
                return
            config = update_interval(minutes, config)
            await update.message.reply_text(
                f"⏱ <b>Interval updated to {minutes} minutes</b>\n"
                f"Auto-scan: {'ENABLED' if config.get('auto_scan',{}).get('enabled') else 'DISABLED'}",
                parse_mode="HTML",
            )
            return

        # Toggle watch alerts
        if arg == "watches":
            sub = args[1] if len(args) > 1 else ""
            auto = config.get("auto_scan", {})
            if sub == "on":
                auto["send_watches"] = True
            elif sub == "off":
                auto["send_watches"] = False
            else:
                auto["send_watches"] = not auto.get("send_watches", False)
            config["auto_scan"] = auto
            save_config(config)
            state = "ON" if auto["send_watches"] else "OFF"
            await update.message.reply_text(f"👀 WATCH alerts: <b>{state}</b>", parse_mode="HTML")
            return

    # Show status
    status = get_status()
    enabled_str  = "✅ ENABLED" if status["enabled"] else "⏸ DISABLED"
    next_str     = status["next_run"] or "—"
    watches_str  = "ON" if status["send_watches"] else "OFF"

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
        [
            InlineKeyboardButton(
                f"👀 WATCH alerts: {watches_str}",
                callback_data="autoscan:watches",
            ),
        ],
    ])

    markets_str = ", ".join(status["markets"])
    await update.message.reply_text(
        f"🤖 <b>Auto-Scanner Status</b>\n\n"
        f"State:     {enabled_str}\n"
        f"Interval:  every {status['interval_minutes']} minutes\n"
        f"Markets:   {markets_str}\n"
        f"Min conf:  {status['min_confidence']}%\n"
        f"Dedup win: {status['dedup_window']} min\n"
        f"WATCH alerts: {watches_str}\n"
        f"Next run:  {next_str}\n\n"
        f"<b>Commands:</b>\n"
        f"/autoscan on · off · 5m · 15m · 30m · 1h",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


def _session_label() -> str:
    from utils.market_hours import get_session_label
    return get_session_label()
