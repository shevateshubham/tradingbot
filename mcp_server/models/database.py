"""
models/database.py — PostgreSQL schema and async database engine.
All tables defined here. Tables are created automatically on first startup.
"""

import logging
from datetime import datetime, date
from typing import Optional, List, Any

import asyncpg
from sqlalchemy import (
    Column, Integer, Float, Boolean, Text, String,
    DateTime, Date, ForeignKey, JSON, func, Index,
    UniqueConstraint, select, update, insert,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession, AsyncEngine, create_async_engine, async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase, relationship

from mcp_server.config import get_settings

logger = logging.getLogger(__name__)

# ── Engine singleton ──────────────────────────────────────────────

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker] = None


def get_engine() -> AsyncEngine:
    """Return cached async engine, creating it if needed."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=300,
            echo=False,
        )
    return _engine


def get_session_factory() -> async_sessionmaker:
    """Return cached session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields an async database session."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Base model ────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


# ── Tables ────────────────────────────────────────────────────────

class UserPreferences(Base):
    """Stores per-user bot configuration and market selections."""
    __tablename__ = "user_preferences"

    user_id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(Text, nullable=False, unique=True, index=True)
    markets_enabled = Column(JSON, default=list, nullable=False)
    timeframe_mode = Column(String(20), default="INTRADAY", nullable=False)
    quality_filter = Column(String(30), default="A_AND_APLUS", nullable=False)
    max_signals_per_day = Column(Integer, default=5, nullable=False)
    signals_paused = Column(Boolean, default=False, nullable=False)
    account_size = Column(Integer, default=100_000, nullable=False)
    risk_percent = Column(Float, default=1.0, nullable=False)
    paper_mode = Column(Boolean, default=True, nullable=False)
    setup_complete = Column(Boolean, default=False, nullable=False)
    consecutive_losses = Column(Integer, default=0, nullable=False)
    pause_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    signal_counts = relationship("UserSignalCount", back_populates="user", cascade="all, delete-orphan")


class UserSignalCount(Base):
    """Daily signal count per user — enforces max_signals_per_day."""
    __tablename__ = "user_signal_count"
    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_user_date"),
    )

    user_id = Column(Integer, ForeignKey("user_preferences.user_id"), nullable=False)
    date = Column(Date, nullable=False, default=date.today)
    signals_sent = Column(Integer, default=0, nullable=False)

    # Composite PK defined via constraint above
    id = Column(Integer, primary_key=True, autoincrement=True)

    # Relationships
    user = relationship("UserPreferences", back_populates="signal_counts")


class Signal(Base):
    """Every signal that passes filters and is sent to Telegram."""
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=func.now(), nullable=False, index=True)
    chat_id = Column(Text, nullable=False, index=True)
    instrument = Column(Text, nullable=False)
    segment = Column(Text, nullable=False)
    direction = Column(Text, nullable=False)     # LONG / SHORT
    timeframe = Column(Text, nullable=False)
    session = Column(Text, nullable=True)
    killzone_active = Column(Boolean, default=False)
    setup_type = Column(Text, nullable=True)
    score = Column(Integer, nullable=False)
    grade = Column(Text, nullable=False)         # A+, A, B, C

    # Trade levels
    entry = Column(Float, nullable=True)
    sl = Column(Float, nullable=True)
    tp1 = Column(Float, nullable=True)
    tp2 = Column(Float, nullable=True)
    tp3 = Column(Float, nullable=True)
    sl_points = Column(Float, nullable=True)
    sl_percent = Column(Float, nullable=True)
    lots = Column(Integer, nullable=True)
    charges = Column(Float, nullable=True)
    net_at_tp1 = Column(Float, nullable=True)

    # Confluence details
    confluences = Column(JSON, default=dict)
    htf_bias = Column(Text, nullable=True)
    liquidity_swept = Column(Boolean, default=False)
    fvg_present = Column(Boolean, default=False)
    volume_ratio = Column(Float, nullable=True)
    in_discount = Column(Boolean, nullable=True)
    trap_present = Column(Boolean, default=False)
    trap_direction = Column(Text, nullable=True)

    # Options data (indices / FNO only)
    options_pcr = Column(Float, nullable=True)
    options_oi_bias = Column(Text, nullable=True)
    max_pain = Column(Float, nullable=True)
    gex = Column(Float, nullable=True)

    # Outcome tracking
    status = Column(Text, default="open", nullable=False)   # open / closed / expired
    outcome = Column(Text, nullable=True)                   # TP1 / TP2 / TP3 / SL / expired
    exit_price = Column(Float, nullable=True)
    actual_rr = Column(Float, nullable=True)
    net_pnl = Column(Float, nullable=True)
    closed_at = Column(DateTime, nullable=True)

    paper_mode = Column(Boolean, default=True, nullable=False)
    telegram_message_id = Column(Integer, nullable=True)    # for editing the original message


class DiscardedSignal(Base):
    """Every signal that was filtered out — for analysis and improvement."""
    __tablename__ = "discarded_signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    chat_id = Column(Text, nullable=True)
    instrument = Column(Text, nullable=False)
    segment = Column(Text, nullable=True)
    direction = Column(Text, nullable=True)
    timeframe = Column(Text, nullable=True)
    session = Column(Text, nullable=True)
    score = Column(Integer, nullable=True)
    grade = Column(Text, nullable=True)
    rejection_reason = Column(Text, nullable=False)     # Why it was discarded
    would_have_been = Column(Text, nullable=True)       # winner / loser (filled retrospectively)

    # Raw payload stored for analysis
    raw_payload = Column(JSON, default=dict)


class ParameterHistory(Base):
    """Audit log for every parameter change from weekly improvement engine."""
    __tablename__ = "parameter_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    changed_at = Column(DateTime, default=func.now(), nullable=False)
    parameter_name = Column(Text, nullable=False)
    old_value = Column(Text, nullable=False)
    new_value = Column(Text, nullable=False)
    reason = Column(Text, nullable=False)
    trades_analyzed = Column(Integer, nullable=True)
    win_rate_before = Column(Float, nullable=True)
    win_rate_after = Column(Float, nullable=True)
    github_pr_url = Column(Text, nullable=True)
    github_pr_number = Column(Integer, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    reverted = Column(Boolean, default=False)
    reverted_at = Column(DateTime, nullable=True)
    revert_reason = Column(Text, nullable=True)


class DeploymentLog(Base):
    """Audit log for every deployment — used by /rollback command."""
    __tablename__ = "deployment_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    deployed_at = Column(DateTime, default=func.now(), nullable=False)
    git_commit = Column(Text, nullable=True)
    git_branch = Column(Text, nullable=True)
    railway_deployment_id = Column(Text, nullable=True)
    change_summary = Column(Text, nullable=True)
    deployed_by = Column(Text, nullable=True)
    rolled_back = Column(Boolean, default=False)
    rolled_back_at = Column(DateTime, nullable=True)


# ── Database initialization ────────────────────────────────────────

async def init_db() -> None:
    """Create all tables if they don't exist. Called on server startup."""
    engine = get_engine()
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise


async def close_db() -> None:
    """Close database engine. Called on server shutdown."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        logger.info("Database connection closed")


# ── Helper query functions ────────────────────────────────────────

async def get_user_by_chat_id(session: AsyncSession, chat_id: str) -> Optional[UserPreferences]:
    """Fetch user preferences by Telegram chat ID."""
    result = await session.execute(
        select(UserPreferences).where(UserPreferences.chat_id == str(chat_id))
    )
    return result.scalar_one_or_none()


async def get_or_create_user(session: AsyncSession, chat_id: str) -> UserPreferences:
    """Fetch user or create new record with defaults."""
    user = await get_user_by_chat_id(session, chat_id)
    if user is None:
        user = UserPreferences(
            chat_id=str(chat_id),
            markets_enabled=[],
            timeframe_mode="INTRADAY",
            quality_filter="A_AND_APLUS",
            max_signals_per_day=5,
            signals_paused=False,
            paper_mode=True,
            setup_complete=False,
        )
        session.add(user)
        await session.flush()
        logger.info(f"Created new user record for chat_id={chat_id}")
    return user


async def get_signals_sent_today(session: AsyncSession, user_id: int) -> int:
    """Return number of signals sent today for this user."""
    today = date.today()
    result = await session.execute(
        select(UserSignalCount.signals_sent).where(
            UserSignalCount.user_id == user_id,
            UserSignalCount.date == today,
        )
    )
    row = result.scalar_one_or_none()
    return row or 0


async def increment_signals_today(session: AsyncSession, user_id: int) -> int:
    """Increment today's signal count and return new total."""
    today = date.today()

    result = await session.execute(
        select(UserSignalCount).where(
            UserSignalCount.user_id == user_id,
            UserSignalCount.date == today,
        )
    )
    row = result.scalar_one_or_none()

    if row is None:
        new_row = UserSignalCount(user_id=user_id, date=today, signals_sent=1)
        session.add(new_row)
        await session.flush()
        return 1
    else:
        row.signals_sent += 1
        await session.flush()
        return row.signals_sent


async def count_open_trades(session: AsyncSession, chat_id: str) -> int:
    """Count currently open (not closed) signal positions for this user."""
    from sqlalchemy import func as sqlfunc
    result = await session.execute(
        select(sqlfunc.count(Signal.id)).where(
            Signal.chat_id == str(chat_id),
            Signal.status == "open",
        )
    )
    return result.scalar() or 0


async def get_recent_losses(session: AsyncSession, chat_id: str, hours: int = 24) -> int:
    """Count consecutive losses in the last N hours."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=hours)

    result = await session.execute(
        select(Signal.outcome).where(
            Signal.chat_id == str(chat_id),
            Signal.status == "closed",
            Signal.closed_at >= cutoff,
            Signal.outcome == "SL",
        ).order_by(Signal.closed_at.desc()).limit(10)
    )
    rows = result.scalars().all()
    # Count consecutive SL hits from most recent
    count = 0
    for outcome in rows:
        if outcome == "SL":
            count += 1
        else:
            break
    return count


async def get_open_signals(session: AsyncSession, chat_id: str) -> List[Signal]:
    """Fetch all open signals for this user."""
    result = await session.execute(
        select(Signal).where(
            Signal.chat_id == str(chat_id),
            Signal.status == "open",
        ).order_by(Signal.created_at.desc())
    )
    return list(result.scalars().all())


async def get_open_base_symbols(session: AsyncSession, chat_id: str) -> List[str]:
    """Return base symbols for all currently open signals (e.g. 'NIFTY' from 'NSE:NIFTY')."""
    result = await session.execute(
        select(Signal.instrument).where(
            Signal.chat_id == str(chat_id),
            Signal.status == "open",
        )
    )
    symbols = []
    for (instrument,) in result.all():
        if instrument:
            # instrument format: "NSE:NIFTY" or just "NIFTY"
            sym = instrument.split(":")[-1] if ":" in instrument else instrument
            symbols.append(sym.upper())
    return symbols


async def get_performance_stats(
    session: AsyncSession, chat_id: str, days: int = 30
) -> dict:
    """Calculate win rate, RR, P&L for the last N days."""
    from datetime import timedelta

    cutoff = datetime.utcnow() - timedelta(days=days)
    result = await session.execute(
        select(Signal).where(
            Signal.chat_id == str(chat_id),
            Signal.status == "closed",
            Signal.closed_at >= cutoff,
        ).order_by(Signal.closed_at.desc())
    )
    signals: List[Signal] = list(result.scalars().all())

    if not signals:
        return {
            "total": 0, "winners": 0, "losers": 0,
            "win_rate": 0.0, "avg_rr": 0.0, "net_pnl": 0.0,
        }

    winners = [s for s in signals if s.outcome and s.outcome.startswith("TP")]
    losers = [s for s in signals if s.outcome == "SL"]
    win_rate = (len(winners) / len(signals)) * 100 if signals else 0.0
    rr_values = [s.actual_rr for s in signals if s.actual_rr is not None]
    avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0.0
    net_pnl = sum(s.net_pnl for s in signals if s.net_pnl is not None)

    return {
        "total": len(signals),
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": round(win_rate, 1),
        "avg_rr": round(avg_rr, 2),
        "net_pnl": round(net_pnl, 2),
    }
