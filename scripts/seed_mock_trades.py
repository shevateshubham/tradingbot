"""
scripts/seed_mock_trades.py — Insert 30 realistic closed mock trades.
Run this to enable weekly self-improvement analysis testing.
Usage: python scripts/seed_mock_trades.py
"""

import asyncio
import logging
import os
import random
import sys
from datetime import datetime, timedelta

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp_server.config import get_settings, setup_logging
from mcp_server.models.database import init_db, get_session_factory, Signal, UserPreferences, get_or_create_user

setup_logging()
logger = logging.getLogger(__name__)

INSTRUMENTS = [
    ("NSE:NIFTY",      "INDICES",       50),
    ("NSE:BANKNIFTY",  "INDICES",       15),
    ("NSE:RELIANCE",   "INDIAN_FNO",   250),
    ("NSE:HDFCBANK",   "INDIAN_FNO",   550),
    ("NSE:INFY",       "INDIAN_FNO",   300),
    ("BINANCE:BTCUSDT","CRYPTO",          1),
    ("MCX:GOLD1!",     "COMMODITY",    100),
]

SESSIONS  = ["INDIA", "LONDON", "NY", "ASIA"]
SETUPS    = ["Bullish OB + FVG", "Bearish OB + FVG", "Bullish OB", "Bearish OB", "CHOCH"]
TIMEFRAMES= ["15", "30", "60"]

# Realistic outcome distribution: ~55% winners
OUTCOME_WEIGHTS = [
    ("TP1", 0.30),
    ("TP2", 0.15),
    ("TP3", 0.10),
    ("SL",  0.45),
]

def pick_outcome() -> str:
    r = random.random()
    cumulative = 0.0
    for outcome, weight in OUTCOME_WEIGHTS:
        cumulative += weight
        if r < cumulative:
            return outcome
    return "SL"

def make_trade(chat_id: str, days_ago: int) -> Signal:
    instrument, segment, lot_size = random.choice(INSTRUMENTS)
    direction  = random.choice(["LONG", "SHORT"])
    session    = random.choice(SESSIONS)
    setup      = random.choice(SETUPS)
    timeframe  = random.choice(TIMEFRAMES)
    score      = random.randint(72, 96)
    grade      = "A+" if score >= 90 else "A"
    outcome    = pick_outcome()

    # Realistic prices
    base_prices = {
        "NSE:NIFTY":      22500.0,
        "NSE:BANKNIFTY":  48000.0,
        "NSE:RELIANCE":    2900.0,
        "NSE:HDFCBANK":    1600.0,
        "NSE:INFY":        1800.0,
        "BINANCE:BTCUSDT": 65000.0,
        "MCX:GOLD1!":      72000.0,
    }
    base = base_prices.get(instrument, 1000.0)
    entry = base * (1 + random.uniform(-0.02, 0.02))
    sl_pts = entry * random.uniform(0.006, 0.012)
    sl    = entry - sl_pts if direction == "LONG" else entry + sl_pts
    tp1   = entry + sl_pts if direction == "LONG" else entry - sl_pts
    tp2   = entry + sl_pts * 2 if direction == "LONG" else entry - sl_pts * 2
    tp3   = entry + sl_pts * 3 if direction == "LONG" else entry - sl_pts * 3

    # Exit price based on outcome
    exit_map = {"TP1": tp1, "TP2": tp2, "TP3": tp3, "SL": sl}
    exit_price = exit_map[outcome]

    lots = random.randint(1, 3)
    charges = 850.0 if segment in ("INDICES", "INDIAN_FNO") else 120.0

    exit_pts = abs(exit_price - entry)
    gross    = exit_pts * lots * lot_size
    is_win   = outcome.startswith("TP")
    net_pnl  = (gross - charges) if is_win else -(exit_pts * lots * lot_size) - charges
    actual_rr= exit_pts / sl_pts if sl_pts > 0 else 0

    created = datetime.utcnow() - timedelta(days=days_ago, hours=random.randint(0, 8))

    return Signal(
        chat_id=chat_id,
        instrument=instrument,
        segment=segment,
        direction=direction,
        timeframe=timeframe,
        session=session,
        killzone_active=random.random() > 0.5,
        setup_type=setup,
        score=score,
        grade=grade,
        entry=round(entry, 2),
        sl=round(sl, 2),
        tp1=round(tp1, 2),
        tp2=round(tp2, 2),
        tp3=round(tp3, 2),
        sl_points=round(sl_pts, 2),
        sl_percent=round(sl_pts / entry * 100, 3),
        lots=lots,
        charges=charges,
        net_at_tp1=round(gross - charges, 2),
        confluences={
            "htf_bias": random.choice(["BULLISH", "BEARISH"]),
            "poi_type": setup,
            "zone_type": random.choice(["Discount", "Premium"]),
            "liquidity_swept": random.random() > 0.4,
            "killzone": random.random() > 0.5,
            "ltf_choch": random.random() > 0.6,
        },
        htf_bias=direction,
        liquidity_swept=random.random() > 0.4,
        fvg_present=random.random() > 0.5,
        volume_ratio=round(random.uniform(0.8, 2.5), 2),
        options_pcr=round(random.uniform(0.7, 1.5), 3) if segment == "INDICES" else None,
        options_oi_bias=random.choice(["BULLISH", "BEARISH", "NEUTRAL"]) if segment == "INDICES" else None,
        max_pain=round(entry * random.uniform(0.99, 1.01), 2) if segment == "INDICES" else None,
        status="closed",
        outcome=outcome,
        exit_price=round(exit_price, 2),
        actual_rr=round(actual_rr, 3),
        net_pnl=round(net_pnl, 2),
        closed_at=created + timedelta(hours=random.randint(1, 6)),
        paper_mode=True,
        created_at=created,
    )


async def seed_trades(count: int = 35) -> None:
    settings = get_settings()
    chat_id  = settings.telegram_chat_id

    await init_db()
    factory = get_session_factory()

    async with factory() as session:
        user = await get_or_create_user(session, chat_id)
        if not user.setup_complete:
            user.markets_enabled = ["INDICES", "INDIAN_FNO", "INDIAN_STOCKS", "CRYPTO"]
            user.timeframe_mode  = "INTRADAY"
            user.quality_filter  = "A_AND_APLUS"
            user.max_signals_per_day = 5
            user.setup_complete  = True
            user.paper_mode      = True
            await session.flush()

        for i in range(count):
            days_ago = random.randint(0, 28)
            trade = make_trade(chat_id, days_ago)
            session.add(trade)

        await session.commit()

    winners = sum(1 for _ in range(count) if pick_outcome().startswith("TP"))
    logger.info(f"✅ Seeded {count} mock trades for chat_id={chat_id}")
    logger.info(f"   Approximate win rate: ~55% ({int(count * 0.55)} winners expected)")
    logger.info("   Run weekly analysis: POST /trigger-weekly or wait until Sunday 8 PM IST")


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 35
    asyncio.run(seed_trades(count))
