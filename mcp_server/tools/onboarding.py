"""
tools/onboarding.py — 3-step interactive market selection onboarding.
Uses Telegram inline keyboards with toggle behaviour.
All selections saved to PostgreSQL instantly on each tap.
"""

import logging
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.models.database import get_or_create_user, UserPreferences
from mcp_server.config import QUALITY_FILTERS

logger = logging.getLogger(__name__)

# ── Market definitions ─────────────────────────────────────────────

MARKETS = [
    {"id": "INDIAN_STOCKS",  "label": "🇮🇳 Indian Stocks"},
    {"id": "INDIAN_FNO",     "label": "📊 Indian F&O"},
    {"id": "INDICES",        "label": "🏦 Indices (Nifty/BN)"},
    {"id": "COMMODITY",      "label": "🌾 Commodity MCX"},
    {"id": "CURRENCY_FOREX", "label": "💱 Currency / Forex"},
    {"id": "CRYPTO",         "label": "₿ Crypto"},
    {"id": "GLOBAL_INDICES", "label": "🌍 Global Indices"},
]

TIMEFRAME_OPTIONS = [
    {"id": "SCALP",    "label": "⚡ Scalp  (1m - 5m)"},
    {"id": "INTRADAY", "label": "🕐 Intraday  (15m - 1H)"},
    {"id": "SWING",    "label": "📅 Swing  (4H - Daily)"},
    {"id": "ALL",      "label": "📈 All Timeframes"},
]

QUALITY_OPTIONS = [
    {"id": "APLUS_ONLY",  "label": "🎯 Only A+ Setups  (max 2/day, highest quality)"},
    {"id": "A_AND_APLUS", "label": "⭐ A and A+ Setups  (max 5/day)"},
    {"id": "ALL_SCORED",  "label": "📊 All Scored Setups  (up to 8/day, more signals)"},
]

MARKET_LABELS = {m["id"]: m["label"] for m in MARKETS}
TF_LABELS     = {t["id"]: t["label"] for t in TIMEFRAME_OPTIONS}
QF_LABELS     = {q["id"]: q["label"] for q in QUALITY_OPTIONS}


# ── Step 1 — Market Selection ─────────────────────────────────────

def build_market_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    """Build the market selection inline keyboard with toggle state."""
    rows = []
    market_buttons = []

    for i, market in enumerate(MARKETS):
        mid = market["id"]
        is_selected = mid in selected
        tick = "✅" if is_selected else "☐"
        label = f"{tick} {market['label'].split(' ', 1)[1] if ' ' in market['label'] else market['label']}"
        # Preserve emoji in label
        full_label = f"{market['label'].split()[0]} {tick} {' '.join(market['label'].split()[1:])}"
        market_buttons.append(
            InlineKeyboardButton(full_label, callback_data=f"onboard_market_{mid}")
        )
        if len(market_buttons) == 2 or i == len(MARKETS) - 1:
            rows.append(market_buttons[:])
            market_buttons = []

    # All markets shortcut
    all_selected = len(selected) == len(MARKETS)
    rows.append([
        InlineKeyboardButton(
            "⚡ All Markets ✅" if all_selected else "⚡ All Markets ☐",
            callback_data="onboard_market_ALL"
        )
    ])

    # Done button — only active if at least 1 selected
    if selected:
        rows.append([InlineKeyboardButton("✅ Done — Step 2 →", callback_data="onboard_step2")])
    else:
        rows.append([InlineKeyboardButton("⬜ Select at least 1 market first", callback_data="onboard_noop")])

    return InlineKeyboardMarkup(rows)


async def send_step1(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    edit: bool = False,
) -> None:
    """Send or edit Step 1 — market selection."""
    chat_id = str(update.effective_chat.id)
    user = await get_or_create_user(session, chat_id)
    selected = user.markets_enabled or []

    text = (
        "👋 *Welcome to SMC Trading Bot*\n\n"
        "I send Smart Money signals only for markets *YOU choose*\\.\n"
        "Tap to select, tap again to deselect\\.\n\n"
        "Zero indicators\\. Pure price action only\\."
    )

    keyboard = build_market_keyboard(selected)

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text=text, reply_markup=keyboard, parse_mode="MarkdownV2"
        )
    else:
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text(text=text, reply_markup=keyboard, parse_mode="MarkdownV2")


async def handle_market_toggle(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    market_id: str,
) -> None:
    """Toggle a market on/off and refresh Step 1 keyboard."""
    query = update.callback_query
    await query.answer()

    chat_id = str(update.effective_chat.id)
    user = await get_or_create_user(session, chat_id)
    selected: list = list(user.markets_enabled or [])

    if market_id == "ALL":
        if len(selected) == len(MARKETS):
            selected = []  # deselect all
        else:
            selected = [m["id"] for m in MARKETS]  # select all
    else:
        if market_id in selected:
            selected.remove(market_id)
        else:
            selected.append(market_id)

    user.markets_enabled = selected
    await session.flush()

    keyboard = build_market_keyboard(selected)
    text = (
        "👋 *Welcome to SMC Trading Bot*\n\n"
        "I send Smart Money signals only for markets *YOU choose*\\.\n"
        "Tap to select, tap again to deselect\\.\n\n"
        "Zero indicators\\. Pure price action only\\."
    )
    await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="MarkdownV2")


# ── Step 2 — Trading Style ────────────────────────────────────────

def build_timeframe_keyboard(selected_tf: str = "INTRADAY") -> InlineKeyboardMarkup:
    """Build trading style selection keyboard."""
    rows = []
    for tf in TIMEFRAME_OPTIONS:
        tick = "✅ " if tf["id"] == selected_tf else ""
        rows.append([
            InlineKeyboardButton(
                f"{tick}{tf['label']}",
                callback_data=f"onboard_tf_{tf['id']}"
            )
        ])
    rows.append([
        InlineKeyboardButton("← Back", callback_data="onboard_step1"),
        InlineKeyboardButton("✅ Continue →", callback_data="onboard_step3"),
    ])
    return InlineKeyboardMarkup(rows)


async def send_step2(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
) -> None:
    """Send Step 2 — trading style selection."""
    query = update.callback_query
    await query.answer()

    chat_id = str(update.effective_chat.id)
    user = await get_or_create_user(session, chat_id)
    current_tf = user.timeframe_mode or "INTRADAY"

    text = (
        "⏱ *What style of trading do you prefer?*\n\n"
        "This controls which timeframe signals you receive\\.\n"
        "You can change this anytime with /markets\\."
    )
    keyboard = build_timeframe_keyboard(current_tf)
    await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="MarkdownV2")


async def handle_timeframe_select(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    tf_id: str,
) -> None:
    """Save timeframe selection and refresh Step 2."""
    query = update.callback_query
    await query.answer()

    chat_id = str(update.effective_chat.id)
    user = await get_or_create_user(session, chat_id)
    user.timeframe_mode = tf_id
    await session.flush()

    text = (
        "⏱ *What style of trading do you prefer?*\n\n"
        "This controls which timeframe signals you receive\\.\n"
        "You can change this anytime with /markets\\."
    )
    keyboard = build_timeframe_keyboard(tf_id)
    await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="MarkdownV2")


# ── Step 3 — Signal Quality ───────────────────────────────────────

def build_quality_keyboard(selected_qf: str = "A_AND_APLUS") -> InlineKeyboardMarkup:
    """Build quality filter selection keyboard."""
    rows = []
    for qf in QUALITY_OPTIONS:
        tick = "✅ " if qf["id"] == selected_qf else ""
        rows.append([
            InlineKeyboardButton(
                f"{tick}{qf['label']}",
                callback_data=f"onboard_qf_{qf['id']}"
            )
        ])
    rows.append([
        InlineKeyboardButton("← Back", callback_data="onboard_step2"),
        InlineKeyboardButton("✅ Finish Setup →", callback_data="onboard_finish"),
    ])
    return InlineKeyboardMarkup(rows)


async def send_step3(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
) -> None:
    """Send Step 3 — signal quality selection."""
    query = update.callback_query
    await query.answer()

    chat_id = str(update.effective_chat.id)
    user = await get_or_create_user(session, chat_id)
    current_qf = user.quality_filter or "A_AND_APLUS"

    text = (
        "🎯 *How selective should I be?*\n\n"
        "Higher selectivity = fewer signals, better quality\\.\n"
        "Start with A and A\\+ — change anytime via /markets\\."
    )
    keyboard = build_quality_keyboard(current_qf)
    await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="MarkdownV2")


async def handle_quality_select(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    qf_id: str,
) -> None:
    """Save quality filter and refresh Step 3."""
    query = update.callback_query
    await query.answer()

    chat_id = str(update.effective_chat.id)
    user = await get_or_create_user(session, chat_id)
    user.quality_filter = qf_id

    # Set max_signals_per_day based on quality filter
    qf_config = QUALITY_FILTERS.get(qf_id, {})
    user.max_signals_per_day = qf_config.get("max_per_day", 5)
    await session.flush()

    text = (
        "🎯 *How selective should I be?*\n\n"
        "Higher selectivity = fewer signals, better quality\\.\n"
        "Start with A and A\\+ — change anytime via /markets\\."
    )
    keyboard = build_quality_keyboard(qf_id)
    await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="MarkdownV2")


# ── Step 4 — Confirmation ─────────────────────────────────────────

def _escape_md(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


async def send_confirmation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
) -> None:
    """Send Step 4 — confirmation screen."""
    query = update.callback_query
    await query.answer()

    chat_id = str(update.effective_chat.id)
    user = await get_or_create_user(session, chat_id)

    # Mark setup complete
    user.setup_complete = True
    await session.flush()

    # Build market list display
    enabled = user.markets_enabled or []
    market_names = ", ".join(
        MARKET_LABELS.get(m, m).split(" ", 1)[1] if " " in MARKET_LABELS.get(m, m) else MARKET_LABELS.get(m, m)
        for m in enabled
    ) or "None selected"
    market_display = _escape_md(market_names)

    tf_label  = _escape_md(TF_LABELS.get(user.timeframe_mode, user.timeframe_mode))
    qf_label  = _escape_md(QF_LABELS.get(user.quality_filter, user.quality_filter))
    max_sig   = user.max_signals_per_day
    mode_str  = "📄 Paper \\(no real money alerts\\)" if user.paper_mode else "🔴 Live"
    acct_size = _escape_md(f"₹{user.account_size:,}")

    text = (
        "✅ *You are ready\\!*\n\n"
        "📊 *Your Preferences:*\n"
        f"Markets   : {market_display}\n"
        f"Style     : {tf_label}\n"
        f"Quality   : {qf_label}\n"
        f"Max/Day   : {max_sig} signals\n"
        f"Account   : {acct_size}\n"
        f"Mode      : {mode_str}\n\n"
        "Change anytime: /markets\n"
        "Go live when ready: /golive"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Start Receiving Signals", callback_data="onboard_done")],
        [InlineKeyboardButton("⚙️ Change Settings", callback_data="onboard_step1")],
    ])

    await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="MarkdownV2")


async def handle_done(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Final acknowledgement after setup."""
    query = update.callback_query
    await query.answer("🟢 Signals active! Waiting for first setup from TradingView.")

    await query.edit_message_text(
        text=(
            "🟢 *Signals are active\\!*\n\n"
            "I will send you SMC signals as TradingView detects setups\\.\n\n"
            "📋 Useful commands:\n"
            "/markets — change preferences\n"
            "/status — check server health\n"
            "/performance — view win rate\n"
            "/pause — pause all signals\n"
            "/help — full command list\n\n"
            "Running in 📄 *Paper Mode* — change with /golive when ready\\."
        ),
        parse_mode="MarkdownV2",
    )


# ── Callback router ────────────────────────────────────────────────

async def handle_onboarding_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    data: str,
) -> None:
    """
    Route all onboard_* callback queries to the correct handler.
    Called from telegram_bot.py callback_query_handler.
    """
    if data == "onboard_noop":
        await update.callback_query.answer("Please select at least one market first.")
        return

    if data.startswith("onboard_market_"):
        market_id = data.replace("onboard_market_", "")
        await handle_market_toggle(update, context, session, market_id)

    elif data == "onboard_step1":
        await send_step1(update, context, session, edit=True)

    elif data == "onboard_step2":
        await send_step2(update, context, session)

    elif data.startswith("onboard_tf_"):
        tf_id = data.replace("onboard_tf_", "")
        await handle_timeframe_select(update, context, session, tf_id)

    elif data == "onboard_step3":
        await send_step3(update, context, session)

    elif data.startswith("onboard_qf_"):
        qf_id = data.replace("onboard_qf_", "")
        await handle_quality_select(update, context, session, qf_id)

    elif data == "onboard_finish":
        await send_confirmation(update, context, session)

    elif data == "onboard_done":
        await handle_done(update, context)

    else:
        logger.warning(f"Unknown onboarding callback: {data}")
        await update.callback_query.answer()
