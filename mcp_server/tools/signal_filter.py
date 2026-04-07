"""
tools/signal_filter.py — Signal filtering decision tree.
Applies all user preference gates before a signal reaches the scoring engine.
Every rejection is logged to discarded_signals table for analysis.
"""

import logging
from datetime import datetime
from typing import Optional, Tuple

import pytz
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.config import (
    TIMEFRAME_MODES, QUALITY_FILTERS, MAX_CONSECUTIVE_LOSSES,
)
from mcp_server.models.database import (
    get_or_create_user, get_signals_sent_today, count_open_trades,
    get_recent_losses, DiscardedSignal, UserPreferences,
    increment_signals_today,
)

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Map string timeframes from TradingView to integer equivalents
TF_NORMALIZE = {
    "1": 1, "3": 3, "5": 5,
    "15": 15, "30": 30, "60": 60, "1H": 60,
    "240": 240, "4H": 240, "D": "D", "1D": "D",
    "W": "W", "1W": "W",
}


def _normalize_tf(tf_str: str) -> object:
    """Normalize TradingView timeframe string to int or string."""
    return TF_NORMALIZE.get(str(tf_str).upper(), tf_str)


def _timeframe_allowed(tf_raw: str, tf_mode: str) -> bool:
    """Check if incoming timeframe matches user's trading style selection."""
    normalized = _normalize_tf(tf_raw)
    allowed = TIMEFRAME_MODES.get(tf_mode, TIMEFRAME_MODES["ALL"])
    return normalized in allowed


async def _log_discard(
    session: AsyncSession,
    payload: dict,
    reason: str,
    chat_id: Optional[str] = None,
    score: Optional[int] = None,
    grade: Optional[str] = None,
) -> None:
    """Log a rejected signal to the discarded_signals table."""
    discard = DiscardedSignal(
        chat_id=chat_id,
        instrument=payload.get("instrument", "UNKNOWN"),
        segment=payload.get("segment"),
        direction=payload.get("direction"),
        timeframe=str(payload.get("timeframe", "")),
        session=payload.get("session"),
        score=score,
        grade=grade,
        rejection_reason=reason,
        raw_payload=payload,
    )
    session.add(discard)
    try:
        await session.flush()
    except Exception as e:
        logger.warning(f"Could not log discard: {e}")


class FilterResult:
    """Encapsulates the result of the filter pass."""
    def __init__(
        self,
        passed: bool,
        reason: Optional[str] = None,
        user: Optional[UserPreferences] = None,
        send_limit_warning: bool = False,
    ):
        self.passed = passed
        self.reason = reason
        self.user = user
        self.send_limit_warning = send_limit_warning

    def __bool__(self) -> bool:
        return self.passed


async def apply_filters(
    session: AsyncSession,
    chat_id: str,
    payload: dict,
    score: Optional[int] = None,
    grade: Optional[str] = None,
) -> FilterResult:
    """
    Run the complete filter decision tree for a single user.

    Decision tree (in order):
      1. User signals paused
      2. Segment not in user's enabled markets
      3. Timeframe mode mismatch
      4. Quality/score filter
      5. Daily signal limit
      6. Max active open trades
      7. Consecutive loss protection

    Returns FilterResult with passed=True if signal should proceed.
    All rejections are logged to discarded_signals.
    """
    user = await get_or_create_user(session, chat_id)
    instrument = payload.get("instrument", "UNKNOWN")
    segment    = payload.get("segment")
    tf_raw     = str(payload.get("timeframe", "15"))

    # ── 1. Signals paused ─────────────────────────────────────────
    if user.signals_paused:
        # Check if auto-pause time has expired
        if user.pause_until and datetime.utcnow() > user.pause_until:
            user.signals_paused = False
            user.pause_until = None
            await session.flush()
            logger.info(f"Auto-pause expired for chat_id={chat_id}")
        else:
            reason = "signals_paused"
            logger.debug(f"Discarding {instrument}: {reason}")
            await _log_discard(session, payload, reason, chat_id, score, grade)
            return FilterResult(False, reason, user)

    # ── 2. Segment not enabled ────────────────────────────────────
    enabled_markets = user.markets_enabled or []
    if segment and segment not in enabled_markets:
        reason = f"segment_not_enabled:{segment}"
        logger.debug(f"Discarding {instrument}: {reason}")
        await _log_discard(session, payload, reason, chat_id, score, grade)
        return FilterResult(False, reason, user)

    # ── 3. Timeframe mode mismatch ────────────────────────────────
    tf_mode = user.timeframe_mode or "INTRADAY"
    if tf_mode != "ALL" and not _timeframe_allowed(tf_raw, tf_mode):
        reason = f"timeframe_mismatch:tf={tf_raw} mode={tf_mode}"
        logger.debug(f"Discarding {instrument}: {reason}")
        await _log_discard(session, payload, reason, chat_id, score, grade)
        return FilterResult(False, reason, user)

    # ── 4. Quality / score filter ─────────────────────────────────
    quality_filter = user.quality_filter or "A_AND_APLUS"
    qf_config = QUALITY_FILTERS.get(quality_filter, {})
    min_score = qf_config.get("min_score", 72)

    if score is not None and score < min_score:
        reason = f"score_below_threshold:score={score} min={min_score}"
        logger.debug(f"Discarding {instrument}: {reason}")
        await _log_discard(session, payload, reason, chat_id, score, grade)
        return FilterResult(False, reason, user)

    # ── 5. Daily signal limit ─────────────────────────────────────
    max_today = user.max_signals_per_day
    sent_today = await get_signals_sent_today(session, user.user_id)

    if sent_today >= max_today:
        reason = f"daily_limit_reached:{sent_today}/{max_today}"
        logger.info(f"Daily limit reached for chat_id={chat_id}: {sent_today}/{max_today}")
        await _log_discard(session, payload, reason, chat_id, score, grade)
        # Send the warning only exactly when limit is first hit
        send_warning = (sent_today == max_today)
        return FilterResult(False, reason, user, send_limit_warning=send_warning)

    # ── 6. Max active open trades ─────────────────────────────────
    from mcp_server.config import get_settings
    settings = get_settings()
    open_count = await count_open_trades(session, chat_id)
    if open_count >= settings.max_active_trades:
        reason = f"max_active_trades:{open_count}/{settings.max_active_trades}"
        logger.info(f"Max active trades reached for chat_id={chat_id}: {open_count}")
        await _log_discard(session, payload, reason, chat_id, score, grade)
        return FilterResult(False, reason, user)

    # ── 7. Consecutive loss protection ────────────────────────────
    recent_losses = await get_recent_losses(session, chat_id, hours=24)
    if recent_losses >= MAX_CONSECUTIVE_LOSSES:
        from mcp_server.config import CONSECUTIVE_LOSS_PAUSE_HOURS
        reason = f"consecutive_loss_protection:{recent_losses}_losses"
        logger.warning(f"Consecutive loss protection triggered for chat_id={chat_id}")
        # Auto-pause for 24 hours
        if not user.signals_paused:
            user.signals_paused = True
            user.pause_until = datetime.utcnow().replace(tzinfo=None)
            from datetime import timedelta
            user.pause_until = datetime.utcnow() + timedelta(hours=CONSECUTIVE_LOSS_PAUSE_HOURS)
            user.consecutive_losses = recent_losses
            await session.flush()
        await _log_discard(session, payload, reason, chat_id, score, grade)
        return FilterResult(False, reason, user)

    # ── All checks passed ─────────────────────────────────────────
    logger.info(
        f"Signal PASSED filters: {instrument} {payload.get('direction')} "
        f"tf={tf_raw} segment={segment} score={score}"
    )
    return FilterResult(True, None, user)


async def should_send_limit_warning_today(
    session: AsyncSession, chat_id: str
) -> bool:
    """Check if we should send a 'daily limit reached' warning (only once per day)."""
    from sqlalchemy import select
    from mcp_server.models.database import DiscardedSignal
    from datetime import date

    today_start = datetime.combine(date.today(), datetime.min.time())
    result = await session.execute(
        select(DiscardedSignal).where(
            DiscardedSignal.chat_id == chat_id,
            DiscardedSignal.rejection_reason.like("daily_limit_reached%"),
            DiscardedSignal.created_at >= today_start,
        ).limit(1)
    )
    # If there's already a record, warning was sent
    row = result.scalar_one_or_none()
    return row is None  # True = first time today, send the warning
