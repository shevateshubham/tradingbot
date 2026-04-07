"""
tools/telegram_bot.py — Telegram bot: all commands, signal formatting,
quick-action keyboards, daily summaries, outcome notifications.
Secures all commands to authorized TELEGRAM_CHAT_ID only.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import pytz
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    Bot, Message,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters,
)
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.config import (
    get_settings, MARKET_LABELS, SEGMENTS,
    QUALITY_FILTERS, TF_LABELS, QF_LABELS,
)
from mcp_server.models.database import (
    get_session_factory, get_or_create_user, get_user_by_chat_id,
    get_performance_stats, get_open_signals, get_signals_sent_today,
    UserPreferences, Signal,
)
from mcp_server.tools.onboarding import (
    send_step1, handle_onboarding_callback,
    MARKET_LABELS as ONBOARD_MARKET_LABELS,
    TF_LABELS as ONBOARD_TF_LABELS,
    QF_LABELS as ONBOARD_QF_LABELS,
)
from mcp_server.tools.rollback_manager import (
    handle_rollback_command, handle_rollback_callback,
)

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Avoid circular: get labels from onboarding module
_MARKET_LABELS = ONBOARD_MARKET_LABELS
_TF_LABELS     = ONBOARD_TF_LABELS
_QF_LABELS     = ONBOARD_QF_LABELS


# ── Security guard ────────────────────────────────────────────────

def authorized_only(func):
    """Decorator: silently ignore messages from unauthorized chat IDs."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        settings = get_settings()
        chat_id = str(update.effective_chat.id)
        if chat_id != str(settings.telegram_chat_id):
            logger.warning(f"Unauthorized access attempt from chat_id={chat_id}")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


# ── Markdown escaping ─────────────────────────────────────────────

def esc(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def fmt_price(price: Optional[float], symbol: str = "₹") -> str:
    """Format price with currency symbol."""
    if price is None:
        return "N/A"
    if abs(price) >= 1000:
        return f"{symbol}{price:,.2f}"
    return f"{symbol}{price:.2f}"


# ── Quick-action keyboard (shown after every signal) ──────────────

def quick_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Active", callback_data="cmd_active"),
        InlineKeyboardButton("⏸ Pause",  callback_data="cmd_pause"),
        InlineKeyboardButton("⚙️ Markets", callback_data="cmd_markets"),
        InlineKeyboardButton("↩️ Rollback", callback_data="cmd_rollback"),
    ]])


# ── Signal message formatter ──────────────────────────────────────

def format_signal_message(signal_data: dict) -> str:
    """
    Format a complete signal dict into the Telegram signal message.
    Returns MarkdownV2-escaped string.
    """
    d = signal_data
    direction_emoji = "🟢" if d.get("direction") == "LONG" else "🔴"
    grade = d.get("grade", "A")
    grade_prefix = "🚨 " if grade == "A+" else "⭐ " if grade == "A" else ""
    instrument = esc(d.get("instrument", "UNKNOWN"))
    segment = esc(SEGMENTS.get(d.get("segment", ""), d.get("segment", "")))
    direction = esc(d.get("direction", "LONG"))
    score = d.get("score", 0)
    htf_tf = esc(d.get("htf_timeframe", "4H"))
    ltf_tf = esc(d.get("timeframe", "15"))
    setup_type = esc(d.get("setup_type", "OB Entry"))

    ccy = "₹" if d.get("segment", "").startswith("INDIAN") or d.get("segment") == "INDICES" else "$"
    entry = esc(f"{d.get('entry', 0):,.2f}")
    sl    = esc(f"{d.get('sl', 0):,.2f}")
    tp1   = esc(f"{d.get('tp1', 0):,.2f}")
    tp2   = esc(f"{d.get('tp2', 0):,.2f}")
    tp3   = esc(f"{d.get('tp3', 0):,.2f}")
    sl_pts    = esc(f"{d.get('sl_points', 0):.1f}")
    sl_pct    = esc(f"{d.get('sl_percent', 0):.2f}")
    lots      = d.get("lots", 1)
    charges   = esc(f"{d.get('charges', 0):,.0f}")
    net_tp1   = esc(f"{d.get('net_at_tp1', 0):,.0f}")

    # Confluences
    conf = d.get("confluences", {})
    htf_bias    = conf.get("htf_bias", "N/A")
    poi_type    = conf.get("poi_type", "N/A")
    zone_type   = conf.get("zone_type", "N/A")
    liq_swept   = conf.get("liquidity_swept", False)
    killzone    = conf.get("killzone", False)
    ltf_choch   = conf.get("ltf_choch", False)
    vol_ratio   = d.get("volume_ratio", 0)
    opts_bias   = d.get("options_oi_bias", None)
    pcr         = d.get("options_pcr", None)
    max_pain_v  = d.get("max_pain", None)

    def chk(val: bool) -> str:
        return "✅" if val else "❌"

    # Institutional evidence
    inst_evidence = d.get("institutional_evidence", [])
    inst_section = ""
    if inst_evidence:
        inst_lines = "\n".join([f"  • {esc(str(e))}" for e in inst_evidence[:4]])
        inst_section = f"🏛 *INSTITUTIONAL EVIDENCE*\n{inst_lines}\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

    # Options strategy section
    opts_sig = d.get("options_signal")
    opts_strategy_section = ""
    if opts_sig and hasattr(opts_sig, "valid") and opts_sig.valid:
        from mcp_server.tools.options_strategy import format_options_signal
        opts_str = format_options_signal(opts_sig, lots=1)
        if opts_str:
            opts_strategy_section = esc(opts_str) + "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"

    opts_line = ""
    if opts_bias:
        pcr_str = f"PCR {pcr:.2f} " if pcr else ""
        opts_line = f"  {chk(True)} Options Bias    : {esc(pcr_str)}{esc(opts_bias)}\n"
    else:
        opts_line = f"  ❌ Options Bias    : N/A\n"

    max_pain_line = ""
    if max_pain_v:
        mp_str = esc(f"{max_pain_v:,.2f}")
        pcr_str = esc(f"{pcr:.2f}") if pcr else esc("N/A")
        max_pain_line = f"📊 Max Pain: {mp_str} \\| PCR: {pcr_str}\n"

    trap_line = ""
    trap_present = d.get("trap_present", False)
    trap_dir = d.get("trap_direction", "")
    if trap_present:
        trap_line = f"🪤 Trap Warning   : {esc(trap_dir.upper())} TRAP ZONE ACTIVE\n"
    else:
        trap_line = "🪤 Trap Warning   : NONE\n"

    # Invalidation level
    if d.get("direction") == "LONG":
        inval_dir = "below"
        inval_level = esc(f"{d.get('sl', 0):,.2f}")
    else:
        inval_dir = "above"
        inval_level = esc(f"{d.get('sl', 0):,.2f}")

    # Signal time
    now_ist = datetime.now(IST)
    signal_time = esc(now_ist.strftime("%H:%M IST"))
    expiry = esc((now_ist + timedelta(hours=4)).strftime("%H:%M IST"))

    mode_str = "📄 Paper" if d.get("paper_mode", True) else "🔴 Live"

    narrative = esc(d.get("narrative", f"Price entering {zone_type} zone at {poi_type}. "
                                        f"HTF is {htf_bias}. Waiting for LTF confirmation."))

    msg = (
        f"🎯 *SMC SIGNAL — {instrument} \\[{segment}\\]*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Direction   : {direction_emoji} *{direction}*\n"
        f"⭐ Grade       : *{grade_prefix}{esc(grade)}*\n"
        f"🔢 Score       : *{score}/100*\n"
        f"⏱ Timeframe   : {htf_tf} bias \\| {ltf_tf}m entry\n"
        f"📐 Setup       : {esc(setup_type)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *TRADE LEVELS*\n"
        f"├─ Entry    : *{entry}*  \\(Limit Order\\)\n"
        f"├─ Stop Loss: *{sl}*  \\({sl_pts} pts \\| {sl_pct}%\\)\n"
        f"├─ Target 1 : {tp1}  \\(1:1 → move SL to entry\\)\n"
        f"├─ Target 2 : {tp2}  \\(1:2 → take 50% off\\)\n"
        f"└─ Target 3 : {tp3}  \\(1:3 → trail remainder\\)\n"
        f"\n"
        f"📦 Lots       : {lots} suggested\n"
        f"💸 Charges    : {ccy}{charges} estimated round trip\n"
        f"✅ Net at TP1 : {ccy}{net_tp1} after all charges\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔍 *CONFLUENCES*\n"
        f"  {chk(bool(htf_bias))} HTF Structure   : {esc(str(htf_bias))}\n"
        f"  {chk(bool(poi_type))} POI Type        : {esc(str(poi_type))}\n"
        f"  {chk(bool(zone_type))} Zone            : {esc(str(zone_type))}\n"
        f"  {chk(liq_swept)} Liquidity Swept : {'Yes — SSL taken' if liq_swept else 'No'}\n"
        f"  {chk(killzone)} Killzone        : {esc(d.get('session', 'N/A'))}\n"
        f"  {chk(ltf_choch)} LTF Confirm     : {'CHOCH confirmed' if ltf_choch else 'Pending'}\n"
        f"  {chk(vol_ratio > 1.5)} Volume Confirm  : Ratio {esc(f'{vol_ratio:.1f}')}× average\n"
        f"{opts_line}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Invalidated if : Close {esc(inval_dir)} {inval_level}\n"
        f"{trap_line}"
        f"{max_pain_line}"
        f"🕐 Signal Time    : {signal_time}\n"
        f"⏳ Expires By     : {expiry}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{inst_section}"
        f"{opts_strategy_section}"
        f"📌 {narrative}\n"
        f"\n_{esc(mode_str)}_"
    )
    return msg


def format_outcome_message(signal: Signal, outcome: str, exit_price: float, net_pnl: float) -> str:
    """Format the outcome notification when SL or TP is hit."""
    ccy = "₹" if signal.segment in ("INDICES", "INDIAN_FNO", "INDIAN_STOCKS") else "$"
    is_win = outcome.startswith("TP")
    emoji = "✅" if is_win else "❌"
    label = "TARGET HIT" if is_win else "STOP LOSS HIT"
    instrument = esc(signal.instrument or "")
    pts = abs((exit_price - (signal.entry or 0)))
    sign = "+" if is_win else "-"

    action_map = {
        "TP1": "Move SL to entry / Take partial profits",
        "TP2": "Take 50% off / Trail remainder",
        "TP3": "Close full position",
        "SL":  "Position closed at stop loss",
    }
    action = esc(action_map.get(outcome, "Manage position"))

    mode_str = "📄 Paper" if signal.paper_mode else "🔴 Live"

    return (
        f"{emoji} *{esc(label)} — {instrument}*\n"
        f"Level   : {esc(outcome)}\n"
        f"Exit    : {esc(f'{exit_price:,.2f}')}\n"
        f"Result  : {esc(f'{sign}{pts:.1f} pts')}\n"
        f"Net P&L : {esc(ccy)}{esc(f'{net_pnl:+,.0f}')} after charges\n"
        f"Action  : {action}\n"
        f"Mode    : {esc(mode_str)}"
    )


def format_daily_summary(signals: list, session_name: str) -> str:
    """Format end-of-session daily summary message."""
    total = len(signals)
    winners = [s for s in signals if s.outcome and s.outcome.startswith("TP")]
    losers  = [s for s in signals if s.outcome == "SL"]
    open_sig = [s for s in signals if s.status == "open"]
    win_rate = (len(winners) / (len(winners) + len(losers)) * 100) if (winners or losers) else 0
    net_pnl  = sum(s.net_pnl or 0 for s in signals)
    rr_vals  = [s.actual_rr for s in signals if s.actual_rr is not None]
    avg_rr   = sum(rr_vals) / len(rr_vals) if rr_vals else 0

    best = max(signals, key=lambda s: s.net_pnl or 0, default=None)
    worst = min(signals, key=lambda s: s.net_pnl or 0, default=None)

    now_ist = datetime.now(IST)
    date_str = esc(now_ist.strftime("%d %b %Y"))
    session_str = esc(session_name)
    net_str = esc(f"₹{net_pnl:+,.0f}")
    wr_str = esc(f"{win_rate:.1f}%")
    rr_str = esc(f"1:{avg_rr:.2f}")

    best_line  = f"Best  : {esc(best.instrument or '')} +{esc(f'{best.net_pnl:,.0f}')}\n" if best and best.net_pnl and best.net_pnl > 0 else ""
    worst_line = f"Worst : {esc(worst.instrument or '')} {esc(f'{worst.net_pnl:,.0f}')}\n" if worst and worst.net_pnl and worst.net_pnl < 0 else ""

    return (
        f"📊 *SESSION REPORT — {date_str} \\| {session_str}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Signals Sent   : {total}\n"
        f"✅ Winners     : {len(winners)}  \\|  ❌ Losers : {len(losers)}\n"
        f"Open           : {len(open_sig)}\n"
        f"Win Rate       : {wr_str}\n"
        f"Avg RR         : {rr_str}\n"
        f"Est\\. Net P&L   : {net_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{best_line}"
        f"{worst_line}"
    )


# ── Command handlers ──────────────────────────────────────────────

@authorized_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    factory = get_session_factory()
    async with factory() as session:
        await send_step1(update, context, session, edit=False)
        await session.commit()


@authorized_only
async def cmd_markets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    factory = get_session_factory()
    async with factory() as session:
        chat_id = str(update.effective_chat.id)
        user = await get_or_create_user(session, chat_id)

        enabled = user.markets_enabled or []
        market_lines = ""
        for m_id, m_label in _MARKET_LABELS.items():
            tick = "✅" if m_id in enabled else "☐ "
            market_lines += f"  {tick} {m_label}\n"

        tf_label = esc(_TF_LABELS.get(user.timeframe_mode, user.timeframe_mode))
        qf_label = esc(_QF_LABELS.get(user.quality_filter, user.quality_filter))
        status_str = "🟢 Active" if not user.signals_paused else "⏸ Paused"
        mode_str = "Paper" if user.paper_mode else "🔴 Live"
        acct_str = esc(f"₹{user.account_size:,}")

        text = (
            f"⚙️ *YOUR SIGNAL PREFERENCES*\n\n"
            f"📊 Markets Active:\n{esc(market_lines)}\n"
            f"⏱ Style    : {tf_label}\n"
            f"🎯 Quality  : {qf_label}\n"
            f"📦 Max/Day  : {user.max_signals_per_day} signals\n"
            f"💰 Account  : {acct_str}\n"
            f"🔔 Status   : {esc(status_str)}\n"
            f"📄 Mode     : {esc(mode_str)}"
        )

        pause_label = "▶️ Resume" if user.signals_paused else "⏸ Pause"
        pause_cb    = "cmd_resume" if user.signals_paused else "cmd_pause"
        live_label  = "📄 Switch to Paper" if not user.paper_mode else "🔴 Go Live"
        live_cb     = "cmd_paper" if not user.paper_mode else "cmd_golive_confirm"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✏️ Markets",  callback_data="onboard_step1"),
                InlineKeyboardButton("✏️ Style",    callback_data="onboard_step2_direct"),
                InlineKeyboardButton("✏️ Quality",  callback_data="onboard_step3_direct"),
            ],
            [
                InlineKeyboardButton(pause_label,  callback_data=pause_cb),
                InlineKeyboardButton(live_label,   callback_data=live_cb),
                InlineKeyboardButton("↩️ Rollback", callback_data="cmd_rollback"),
            ],
        ])

        await update.message.reply_text(text=text, reply_markup=keyboard, parse_mode="MarkdownV2")
        await session.commit()


@authorized_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as session:
        chat_id = str(update.effective_chat.id)
        user = await get_or_create_user(session, chat_id)
        today_count = await get_signals_sent_today(session, user.user_id)

        now_ist = datetime.now(IST)
        mode_str = "📄 Paper" if user.paper_mode else "🔴 Live"
        status_str = "🟢 Receiving signals" if not user.signals_paused else "⏸ Paused"

        text = (
            f"📡 *SERVER STATUS*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Bot        : {esc(status_str)}\n"
            f"Mode       : {esc(mode_str)}\n"
            f"Time \\(IST\\): {esc(now_ist.strftime('%H:%M:%S'))}\n"
            f"Env        : {esc(settings.railway_environment)}\n"
            f"Signals today: {today_count}/{user.max_signals_per_day}\n"
            f"DB         : 🟢 Connected\n"
        )
        await update.message.reply_text(text=text, parse_mode="MarkdownV2")
        await session.commit()


@authorized_only
async def cmd_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    factory = get_session_factory()
    async with factory() as session:
        chat_id = str(update.effective_chat.id)
        open_signals = await get_open_signals(session, chat_id)

        if not open_signals:
            await update.message.reply_text(
                "📭 No open signals at the moment\\.\nWaiting for next setup from TradingView\\.",
                parse_mode="MarkdownV2",
            )
            return

        lines = [f"📊 *OPEN SIGNALS \\({len(open_signals)}\\)*\n"]
        for sig in open_signals:
            direction_emoji = "🟢" if sig.direction == "LONG" else "🔴"
            age_hours = int((datetime.utcnow() - sig.created_at).total_seconds() // 3600) if sig.created_at else 0
            lines.append(
                f"{direction_emoji} *{esc(sig.instrument or '')}* — {esc(sig.grade or 'A')}\n"
                f"  Entry: {esc(f'{sig.entry:,.2f}' if sig.entry else 'N/A')} "
                f"| SL: {esc(f'{sig.sl:,.2f}' if sig.sl else 'N/A')} "
                f"| TP1: {esc(f'{sig.tp1:,.2f}' if sig.tp1 else 'N/A')}\n"
                f"  Opened {age_hours}h ago\n"
            )

        await update.message.reply_text(
            "\n".join(lines), parse_mode="MarkdownV2", reply_markup=quick_keyboard()
        )
        await session.commit()


@authorized_only
async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    factory = get_session_factory()
    async with factory() as session:
        chat_id = str(update.effective_chat.id)
        stats = await get_performance_stats(session, chat_id, days=30)

        if stats["total"] == 0:
            await update.message.reply_text(
                "📊 No closed trades yet\\.\nSignals will appear here after first trade closes\\.",
                parse_mode="MarkdownV2",
            )
            return

        net_str = esc(f"₹{stats['net_pnl']:+,.0f}")
        wr_str  = esc(f"{stats['win_rate']:.1f}%")
        rr_str  = esc(f"1:{stats['avg_rr']:.2f}")

        text = (
            f"📈 *PERFORMANCE — Last 30 Days*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Total Trades : {stats['total']}\n"
            f"✅ Winners   : {stats['winners']}\n"
            f"❌ Losers    : {stats['losers']}\n"
            f"Win Rate     : *{wr_str}*\n"
            f"Avg RR       : {rr_str}\n"
            f"Net P&L      : *{net_str}*\n"
        )
        await update.message.reply_text(text=text, parse_mode="MarkdownV2")
        await session.commit()


@authorized_only
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from sqlalchemy import select
    from datetime import date

    factory = get_session_factory()
    async with factory() as session:
        chat_id = str(update.effective_chat.id)
        today_start = datetime.combine(date.today(), datetime.min.time())

        result = await session.execute(
            select(Signal).where(
                Signal.chat_id == chat_id,
                Signal.created_at >= today_start,
            ).order_by(Signal.created_at.desc())
        )
        today_signals = list(result.scalars().all())

        if not today_signals:
            await update.message.reply_text(
                "📭 No signals sent today yet\\.", parse_mode="MarkdownV2"
            )
            return

        lines = [f"📅 *TODAY'S SIGNALS \\({len(today_signals)}\\)*\n"]
        for sig in today_signals:
            status_emoji = {"open": "🔵", "closed": "✅" if sig.outcome and "TP" in sig.outcome else "❌", "expired": "⏰"}.get(sig.status, "⚪")
            lines.append(
                f"{status_emoji} {esc(sig.direction or '')} *{esc(sig.instrument or '')}* "
                f"\\| {esc(sig.grade or '')} "
                f"\\| {esc(sig.status or '')}"
                + (f" → {esc(sig.outcome)}" if sig.outcome else "")
            )

        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")
        await session.commit()


@authorized_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    factory = get_session_factory()
    async with factory() as session:
        chat_id = str(update.effective_chat.id)
        user = await get_or_create_user(session, chat_id)
        user.signals_paused = True
        await session.commit()
        await update.message.reply_text(
            "⏸ *Signals paused*\\.\nSend /resume to restart\\.",
            parse_mode="MarkdownV2",
        )


@authorized_only
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    factory = get_session_factory()
    async with factory() as session:
        chat_id = str(update.effective_chat.id)
        user = await get_or_create_user(session, chat_id)
        user.signals_paused = False
        user.pause_until = None
        await session.commit()
        await update.message.reply_text(
            "🟢 *Signals resumed*\\. Watching for next setup\\.",
            parse_mode="MarkdownV2",
        )


@authorized_only
async def cmd_golive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, go live", callback_data="cmd_golive_confirm"),
        InlineKeyboardButton("❌ Cancel",       callback_data="cmd_cancel"),
    ]])
    await update.message.reply_text(
        "⚠️ *Switch to Live Mode?*\n\n"
        "All signals will be marked 🔴 Live\\.\n"
        "P&L tracking will reflect real outcomes\\.\n\n"
        "Make sure you have reviewed paper mode performance first\\!\n"
        "Send /performance to check before proceeding\\.",
        reply_markup=keyboard,
        parse_mode="MarkdownV2",
    )


@authorized_only
async def cmd_setaccount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Usage: /setaccount 500000\nEnter your account size as a number\\.",
            parse_mode="MarkdownV2",
        )
        return

    amount = int(args[0])
    if amount < 10000:
        await update.message.reply_text(
            "❌ Account size must be at least ₹10,000\\.", parse_mode="MarkdownV2"
        )
        return

    factory = get_session_factory()
    async with factory() as session:
        chat_id = str(update.effective_chat.id)
        user = await get_or_create_user(session, chat_id)
        user.account_size = amount
        await session.commit()
        await update.message.reply_text(
            f"✅ Account size set to *₹{amount:,}*\\.\nPosition sizing will update on next signal\\.",
            parse_mode="MarkdownV2",
        )


@authorized_only
async def cmd_rollback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    factory = get_session_factory()
    async with factory() as session:
        await handle_rollback_command(update, context, session)
        await session.commit()


@authorized_only
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve a weekly improvement PR: /approve 12"""
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /approve 12  \\(PR number\\)", parse_mode="MarkdownV2")
        return
    pr_number = int(args[0])
    from mcp_server.tools.performance import approve_improvement_pr
    factory = get_session_factory()
    async with factory() as session:
        await approve_improvement_pr(update, context, session, pr_number)
        await session.commit()


@authorized_only
async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reject a weekly improvement PR: /reject 12"""
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /reject 12  \\(PR number\\)", parse_mode="MarkdownV2")
        return
    pr_number = int(args[0])
    from mcp_server.tools.performance import reject_improvement_pr
    factory = get_session_factory()
    async with factory() as session:
        await reject_improvement_pr(update, context, session, pr_number)
        await session.commit()


@authorized_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📋 *SMC TRADING BOT — COMMANDS*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "/start          → Market selection onboarding\n"
        "/markets        → View and edit all preferences\n"
        "/status         → Server health and uptime\n"
        "/active         → Open signals with levels\n"
        "/performance    → Last 30 days win rate, P&L\n"
        "/today          → Today's signals and outcomes\n"
        "/pause          → Pause all signals\n"
        "/resume         → Resume signals\n"
        "/golive         → Switch to live mode\n"
        "/setaccount X   → Set account size \\(e\\.g\\. 500000\\)\n"
        "/rollback       → Restore a previous version\n"
        "/approve N      → Approve weekly improvement PR\n"
        "/reject N       → Reject weekly improvement PR\n"
        "/help           → This command list\n"
    )
    await update.message.reply_text(text=text, parse_mode="MarkdownV2")


# ── Callback query router ─────────────────────────────────────────

@authorized_only
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route all callback_query events to the correct handler."""
    query = update.callback_query
    data = query.data or ""

    factory = get_session_factory()
    async with factory() as session:
        chat_id = str(update.effective_chat.id)

        # Onboarding callbacks
        if data.startswith("onboard_"):
            await handle_onboarding_callback(update, context, session, data)

        # Rollback callbacks
        elif data.startswith("rollback_"):
            dep_id = data.replace("rollback_", "")
            await handle_rollback_callback(update, context, session, dep_id)

        # Quick action: active
        elif data == "cmd_active":
            await query.answer()
            open_signals = await get_open_signals(session, chat_id)
            if not open_signals:
                await query.answer("📭 No open signals.", show_alert=True)
            else:
                lines = [f"📊 Open Signals ({len(open_signals)})"]
                for sig in open_signals:
                    emoji = "🟢" if sig.direction == "LONG" else "🔴"
                    lines.append(f"{emoji} {sig.instrument} | {sig.grade} | Entry {sig.entry:,.2f}")
                await query.answer("\n".join(lines[:3]), show_alert=True)

        # Quick action: pause
        elif data == "cmd_pause":
            user = await get_or_create_user(session, chat_id)
            user.signals_paused = True
            await session.flush()
            await query.answer("⏸ Signals paused. Send /resume to restart.")
            await query.edit_message_reply_markup(reply_markup=None)

        # Quick action: resume
        elif data == "cmd_resume":
            user = await get_or_create_user(session, chat_id)
            user.signals_paused = False
            await session.flush()
            await query.answer("🟢 Signals resumed.")

        # Quick action: markets (re-open onboarding step 1)
        elif data in ("cmd_markets", "onboard_step2_direct"):
            await send_step1(update, context, session, edit=True)

        elif data == "onboard_step3_direct":
            from mcp_server.tools.onboarding import send_step3
            await send_step3(update, context, session)

        # Go live confirm
        elif data == "cmd_golive_confirm":
            user = await get_or_create_user(session, chat_id)
            user.paper_mode = False
            await session.flush()
            await query.answer("🔴 Switched to Live Mode!")
            await query.edit_message_text(
                "🔴 *Live Mode Activated*\n\nAll signals are now marked Live\\.\n"
                "Manage your positions carefully\\!\n/paper to switch back\\.",
                parse_mode="MarkdownV2",
            )

        elif data == "cmd_paper":
            user = await get_or_create_user(session, chat_id)
            user.paper_mode = True
            await session.flush()
            await query.answer("📄 Switched to Paper Mode.")
            await query.edit_message_text(
                "📄 *Paper Mode Activated*\n\nSignals are informational only\\.",
                parse_mode="MarkdownV2",
            )

        elif data == "cmd_cancel" or data == "cmd_rollback":
            await query.answer()
            if data == "cmd_rollback":
                # Trigger rollback command flow
                await query.answer()
                await handle_rollback_command(update, context, session)

        else:
            logger.warning(f"Unhandled callback: {data}")
            await query.answer()

        await session.commit()


# ── Public signal sender (called by pipeline) ─────────────────────

async def send_signal_to_telegram(bot: Bot, chat_id: str, signal_data: dict) -> Optional[int]:
    """
    Format and send a complete signal message to Telegram.
    Returns the message_id so it can be stored for later editing.
    """
    try:
        text = format_signal_message(signal_data)
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="MarkdownV2",
            reply_markup=quick_keyboard(),
        )
        logger.info(
            f"Signal sent: {signal_data.get('instrument')} "
            f"{signal_data.get('direction')} score={signal_data.get('score')}"
        )
        return msg.message_id
    except Exception as e:
        logger.error(f"Failed to send signal to Telegram: {e}")
        return None


async def send_outcome_to_telegram(
    bot: Bot, chat_id: str, signal: Signal, outcome: str, exit_price: float, net_pnl: float
) -> None:
    """Send outcome notification when SL or TP is hit."""
    try:
        text = format_outcome_message(signal, outcome, exit_price, net_pnl)
        await bot.send_message(
            chat_id=chat_id, text=text,
            parse_mode="MarkdownV2",
            reply_markup=quick_keyboard(),
        )
    except Exception as e:
        logger.error(f"Failed to send outcome to Telegram: {e}")


async def send_daily_limit_warning(bot: Bot, chat_id: str, limit: int) -> None:
    """Send once when daily signal limit is reached."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"📊 Daily limit reached \\(*{limit}/{limit}* signals\\)\\.\n"
                "Filter is protecting you from overtrading\\.\n"
                "Resumes tomorrow\\. Change limit via /markets"
            ),
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error(f"Failed to send limit warning: {e}")


async def send_consecutive_loss_alert(bot: Bot, chat_id: str, count: int) -> None:
    """Alert when consecutive loss protection triggers."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ *Consecutive Loss Protection Activated*\n\n"
                f"{count} consecutive losses in the last 24 hours\\.\n"
                f"Signals paused for 24 hours to protect your capital\\.\n\n"
                f"Use /resume to override \\(not recommended\\)\\.\n"
                f"Review your market selection with /markets\\."
            ),
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error(f"Failed to send consecutive loss alert: {e}")


# ── Application builder ────────────────────────────────────────────

def build_telegram_app(token: str) -> Application:
    """Build and configure the Telegram Application with all handlers."""
    app = Application.builder().token(token).build()

    # Command handlers
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("markets",     cmd_markets))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("active",      cmd_active))
    app.add_handler(CommandHandler("performance", cmd_performance))
    app.add_handler(CommandHandler("today",       cmd_today))
    app.add_handler(CommandHandler("pause",       cmd_pause))
    app.add_handler(CommandHandler("resume",      cmd_resume))
    app.add_handler(CommandHandler("golive",      cmd_golive))
    app.add_handler(CommandHandler("setaccount",  cmd_setaccount))
    app.add_handler(CommandHandler("rollback",    cmd_rollback))
    app.add_handler(CommandHandler("approve",     cmd_approve))
    app.add_handler(CommandHandler("reject",      cmd_reject))
    app.add_handler(CommandHandler("help",        cmd_help))

    # Callback query handler (all inline keyboard taps)
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Ignore all other messages from unauthorized users silently
    app.add_handler(MessageHandler(filters.ALL, lambda u, c: None))

    return app
