import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update as sql_update
from mcp_server.models.database import get_or_create_user, UserPreferences
from mcp_server.config import QUALITY_FILTERS
logger = logging.getLogger(__name__)

MARKETS = [
    {"id":"INDICES",        "label":"🏦 Indices (Nifty/BN)",  "hours":"9:15 AM - 3:30 PM IST"},
    {"id":"INDIAN_FNO",     "label":"📊 Indian F&O",           "hours":"9:15 AM - 3:30 PM IST"},
    {"id":"COMMODITY",      "label":"🥇 XAUUSD Gold",          "hours":"3:30 PM - 12:00 AM IST"},
    {"id":"CURRENCY_FOREX", "label":"💱 Forex Majors",         "hours":"3:30 PM - 12:00 AM IST"},
    {"id":"INDIAN_STOCKS",  "label":"🇮🇳 Indian Stocks",       "hours":"9:15 AM - 3:30 PM IST"},
    {"id":"CRYPTO",         "label":"₿ Crypto",                "hours":"24/7"},
    {"id":"GLOBAL_INDICES", "label":"🌍 Global Indices",       "hours":"Evening"},
]
TIMEFRAME_OPTIONS = [
    {"id":"SCALP",    "label":"⚡ Scalp  (1m - 5m)"},
    {"id":"INTRADAY", "label":"🕐 Intraday  (15m - 1H)"},
    {"id":"SWING",    "label":"📅 Swing  (4H - Daily)"},
    {"id":"ALL",      "label":"📈 All Timeframes"},
]
QUALITY_OPTIONS = [
    {"id":"APLUS_ONLY",  "label":"🎯 Only A+ Setups  (max 2/day)"},
    {"id":"A_AND_APLUS", "label":"⭐ A and A+ Setups  (max 5/day)"},
    {"id":"ALL_SCORED",  "label":"📊 All Scored Setups  (up to 8/day)"},
]
MARKET_LABELS = {m["id"]:m["label"] for m in MARKETS}
TF_LABELS     = {t["id"]:t["label"] for t in TIMEFRAME_OPTIONS}
QF_LABELS     = {q["id"]:q["label"] for q in QUALITY_OPTIONS}

def build_market_keyboard(selected):
    rows=[]
    mbtns=[]
    for i,market in enumerate(MARKETS):
        mid=market["id"];tick="✅" if mid in selected else "☐"
        parts=market["label"].split();emoji=parts[0];rest=" ".join(parts[1:])
        mbtns.append(InlineKeyboardButton(f"{emoji} {tick} {rest}",callback_data=f"onboard_market_{mid}"))
        if len(mbtns)==2 or i==len(MARKETS)-1:
            rows.append(mbtns[:]);mbtns=[]
    all_sel=len(selected)==len(MARKETS)
    rows.append([InlineKeyboardButton("⚡ All Markets ✅" if all_sel else "⚡ All Markets ☐",callback_data="onboard_market_ALL")])
    if selected:
        rows.append([InlineKeyboardButton("✅ Done — Step 2 →",callback_data="onboard_step2")])
    else:
        rows.append([InlineKeyboardButton("⬜ Select at least 1 market first",callback_data="onboard_noop")])
    return InlineKeyboardMarkup(rows)

async def send_step1(update,context,session,edit=False):
    chat_id=str(update.effective_chat.id)
    user=await get_or_create_user(session,chat_id)
    selected=user.markets_enabled or []
    text=(
        "👋 Welcome to SMC Trading Bot\n\n"
        "I scan markets using institutional SMC analysis:\n"
        "• Wyckoff Method • Volume Analysis\n"
        "• Liquidity Engineering • Trap Detection\n\n"
        "Market Schedule:\n"
        "🏦 Indices + F&O: 9:15 AM - 3:30 PM IST\n"
        "🥇 Gold + Forex: 3:30 PM - 12:00 AM IST\n"
        "😴 Sleep: 12:00 AM - 9:15 AM IST\n\n"
        "Select your markets below:"
    )
    kb=build_market_keyboard(selected)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text,reply_markup=kb)
    else:
        target=update.message or update.callback_query.message
        await target.reply_text(text,reply_markup=kb)

def build_tf_keyboard(selected_tf):
    rows=[]
    for tf in TIMEFRAME_OPTIONS:
        tick="✅" if tf["id"]==selected_tf else "☐"
        rows.append([InlineKeyboardButton(f"{tick} {tf['label']}",callback_data=f"onboard_tf_{tf['id']}")])
    done_btn=InlineKeyboardButton("✅ Continue →",callback_data="onboard_step3") if selected_tf else InlineKeyboardButton("⬜ Select style first",callback_data="onboard_noop")
    rows.append([InlineKeyboardButton("← Back",callback_data="onboard_step1"),done_btn])
    return InlineKeyboardMarkup(rows)

async def send_step2(update,context,session,edit=True):
    chat_id=str(update.effective_chat.id)
    user=await get_or_create_user(session,chat_id)
    text="⏱ What style of trading do you prefer?\n\nThis affects which timeframe signals you receive."
    kb=build_tf_keyboard(user.timeframe_mode or "")
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text,reply_markup=kb)
    else:
        target=update.message or update.callback_query.message
        await target.reply_text(text,reply_markup=kb)

def build_qf_keyboard(selected_qf):
    rows=[]
    for qf in QUALITY_OPTIONS:
        tick="✅" if qf["id"]==selected_qf else "☐"
        rows.append([InlineKeyboardButton(f"{tick} {qf['label']}",callback_data=f"onboard_qf_{qf['id']}")])
    done_btn=InlineKeyboardButton("✅ Finish →",callback_data="onboard_step4") if selected_qf else InlineKeyboardButton("⬜ Select quality first",callback_data="onboard_noop")
    rows.append([InlineKeyboardButton("← Back",callback_data="onboard_step2"),done_btn])
    return InlineKeyboardMarkup(rows)

async def send_step3(update,context,session,edit=True):
    chat_id=str(update.effective_chat.id)
    user=await get_or_create_user(session,chat_id)
    text="🎯 How selective should I be?\n\nHigher quality = fewer signals but better setups.\nAll signals still go through full institutional analysis."
    kb=build_qf_keyboard(user.quality_filter or "")
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text,reply_markup=kb)
    else:
        target=update.message or update.callback_query.message
        await target.reply_text(text,reply_markup=kb)

async def send_step4(update,context,session,edit=True):
    chat_id=str(update.effective_chat.id)
    user=await get_or_create_user(session,chat_id)
    markets=user.markets_enabled or []
    tf=user.timeframe_mode or "INTRADAY"
    qf=user.quality_filter or "A_AND_APLUS"
    qf_data=QUALITY_FILTERS.get(qf,{})
    max_sigs=qf_data.get("max_signals",5)
    market_labels=", ".join([MARKET_LABELS.get(m,m) for m in markets]) or "None selected"
    mode="📄 Paper" if user.paper_mode else "🔴 Live"
    text=(
        f"✅ You are ready!\n\n"
        f"Markets   : {market_labels}\n"
        f"Style     : {TF_LABELS.get(tf,tf)}\n"
        f"Quality   : {QF_LABELS.get(qf,qf)}\n"
        f"Max/Day   : {max_sigs} signals\n"
        f"Mode      : {mode}\n\n"
        f"🏦 Indian session: 9:15 AM - 3:30 PM IST\n"
        f"🥇 Evening session: 3:30 PM - 12:00 AM IST\n\n"
        f"I will send signals automatically when institutional setups are detected!"
    )
    kb=InlineKeyboardMarkup([[
        InlineKeyboardButton("🟢 Start Signals",callback_data="onboard_confirm"),
        InlineKeyboardButton("⚙️ Change Settings",callback_data="onboard_step1")
    ]])
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text,reply_markup=kb)
    else:
        target=update.message or update.callback_query.message
        await target.reply_text(text,reply_markup=kb)

async def handle_onboarding_callback(update,context,session,data=None):
    query=update.callback_query
    await query.answer()
    if data is None: data=query.data
    chat_id=str(update.effective_chat.id)
    user=await get_or_create_user(session,chat_id)

    if data=="onboard_noop":
        return
    elif data.startswith("onboard_market_"):
        mid=data.replace("onboard_market_","")
        selected=list(user.markets_enabled or [])
        if mid=="ALL":
            selected=[m["id"] for m in MARKETS] if len(selected)<len(MARKETS) else []
        else:
            if mid in selected: selected.remove(mid)
            else: selected.append(mid)
        await session.execute(sql_update(UserPreferences).where(UserPreferences.chat_id==chat_id).values(markets_enabled=selected))
        await session.commit()
        user.markets_enabled=selected
        await send_step1(update,context,session,edit=True)
    elif data in ("onboard_step1","onboard_step1_direct"):
        await send_step1(update,context,session,edit=True)
    elif data in ("onboard_step2","onboard_step2_direct"):
        await send_step2(update,context,session,edit=True)
    elif data in ("onboard_step3","onboard_step3_direct"):
        await send_step3(update,context,session,edit=True)
    elif data=="onboard_step4":
        await send_step4(update,context,session,edit=True)
    elif data.startswith("onboard_tf_"):
        tf=data.replace("onboard_tf_","")
        await session.execute(sql_update(UserPreferences).where(UserPreferences.chat_id==chat_id).values(timeframe_mode=tf))
        await session.commit()
        user.timeframe_mode=tf
        await send_step2(update,context,session,edit=True)
    elif data.startswith("onboard_qf_"):
        qf=data.replace("onboard_qf_","")
        qf_data=QUALITY_FILTERS.get(qf,{})
        max_sigs=qf_data.get("max_signals",5)
        await session.execute(sql_update(UserPreferences).where(UserPreferences.chat_id==chat_id).values(quality_filter=qf,max_signals_per_day=max_sigs))
        await session.commit()
        user.quality_filter=qf
        await send_step3(update,context,session,edit=True)
    elif data=="onboard_confirm":
        await session.execute(sql_update(UserPreferences).where(UserPreferences.chat_id==chat_id).values(setup_complete=True))
        await session.commit()
        await query.edit_message_text(
            "🚀 Bot is now active!\n\n"
            "I will automatically send signals when institutional setups are detected.\n\n"
            "📊 Use /markets to change settings anytime\n"
            "📈 Use /status to check bot health\n"
            "📋 Use /help for all commands"
        )

handle_onboard_callback = handle_onboarding_callback
