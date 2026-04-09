"""
mcp_server/server.py — Main entry point.
FastAPI application with:
  - POST /webhook: TradingView signal receiver
  - GET /health: Railway health check
  - Telegram bot polling (background thread)
  - APScheduler: daily summaries + weekly analysis + price tracker
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date
from typing import Optional

import pytz
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from mcp_server.config import (
    get_settings, setup_logging,
    DAILY_SUMMARY_INDIA_TIME, DAILY_SUMMARY_GLOBAL_TIME,
    WEEKLY_ANALYSIS_DAY, WEEKLY_ANALYSIS_TIME,
    PRICE_CHECK_INTERVAL_SECONDS,
)
from mcp_server.models.database import (
    init_db, close_db, get_session_factory,
    get_or_create_user, increment_signals_today,
    Signal, UserPreferences, DiscardedSignal,
)
from mcp_server.tools.webhook_handler import handle_webhook
from mcp_server.tools.segment_detector import detect_segment
from mcp_server.tools.signal_filter import apply_filters
from mcp_server.tools.smc_processor import process_smc_payload
from mcp_server.tools.trade_scorer import score_signal
from mcp_server.tools.trade_calculator import calculate_position
from mcp_server.tools.telegram_bot import (
    build_telegram_app, send_signal_to_telegram,
    send_outcome_to_telegram, send_daily_limit_warning,
    send_consecutive_loss_alert, format_daily_summary,
)
from mcp_server.resources.feeds import fetch_current_price

setup_logging()
logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ── Globals ────────────────────────────────────────────────────────
_telegram_app = None
_scheduler: Optional[AsyncIOScheduler] = None
_consecutive_loss_warned: set = set()  # chat_ids already warned today


# ── Signal processing pipeline ────────────────────────────────────

async def run_signal_pipeline(raw_body: dict) -> dict:
    """
    Complete signal processing pipeline.
    Returns dict with result status.
    """
    settings = get_settings()
    chat_id = settings.telegram_chat_id
    factory = get_session_factory()

    # ── Stage 1: Validate and normalize webhook ───────────────────
    normalized, error = await handle_webhook(raw_body)
    if normalized is None:
        return {"status": "rejected", "reason": error}

    # ── Stage 2: Detect segment ───────────────────────────────────
    segment = normalized.segment
    if segment == "UNKNOWN":
        logger.warning(f"Unknown segment for {normalized.instrument} — discarding")
        return {"status": "rejected", "reason": "unknown_segment"}

    # ── Stage 3: SMC enrichment ───────────────────────────────────
    enriched = await process_smc_payload(normalized)
    enriched["segment"] = segment

    # ── Stage 4: Scoring (pre-filter score) ──────────────────────
    score_result = score_signal(enriched)
    enriched["score"] = score_result.score
    enriched["grade"] = score_result.grade

    # ── Stage 5: User preference filter ──────────────────────────
    async with factory() as session:
        filter_result = await apply_filters(
            session, chat_id, enriched,
            score=score_result.score, grade=score_result.grade,
        )
        await session.commit()

    if not filter_result.passed:
        # Send daily limit warning once
        if filter_result.send_limit_warning:
            user = filter_result.user
            max_signals = user.max_signals_per_day if user else 5
            await send_daily_limit_warning(_telegram_app.bot, chat_id, max_signals)

        # Check if consecutive loss protection triggered (alert user)
        if filter_result.reason and "consecutive_loss_protection" in filter_result.reason:
            if chat_id not in _consecutive_loss_warned:
                _consecutive_loss_warned.add(chat_id)
                count = int(filter_result.reason.split(":")[1].split("_")[0])
                await send_consecutive_loss_alert(_telegram_app.bot, chat_id, count)

        return {"status": "filtered", "reason": filter_result.reason}

    # ── Stage 6: Hard discard check ──────────────────────────────
    if not score_result.passed:
        logger.info(f"Score discard: {score_result.reject_reason}")
        return {"status": "discarded", "reason": score_result.reject_reason}

    # ── Stage 7: Trade calculator ─────────────────────────────────
    async with factory() as session:
        user = filter_result.user or await get_or_create_user(session, chat_id)
        account_size = user.account_size
        risk_percent = user.risk_percent
        paper_mode   = user.paper_mode
        await session.commit()

    calc = calculate_position(enriched, account_size, risk_percent)

    if not calc.valid:
        logger.info(f"Calculator discard: {calc.reject_reason}")
        # Log to discarded_signals
        async with factory() as session:
            disc = DiscardedSignal(
                chat_id=chat_id,
                instrument=enriched.get("instrument"),
                segment=segment,
                direction=enriched.get("direction"),
                timeframe=enriched.get("timeframe"),
                session=enriched.get("session"),
                score=score_result.score,
                grade=score_result.grade,
                rejection_reason=calc.reject_reason or "calculator_reject",
                raw_payload=enriched,
            )
            session.add(disc)
            await session.commit()
        return {"status": "discarded", "reason": calc.reject_reason}

    # ── Stage 8: Build final signal dict ──────────────────────────
    signal_data = {
        **enriched,
        "score":        score_result.score,
        "grade":        score_result.grade,
        "entry":        calc.entry,
        "sl":           calc.sl,
        "tp1":          calc.tp1,
        "tp2":          calc.tp2,
        "tp3":          calc.tp3,
        "sl_points":    calc.sl_points,
        "sl_percent":   calc.sl_percent,
        "rr_ratio":     calc.rr_ratio,
        "lots":         calc.lots,
        "charges":      calc.charges,
        "net_at_tp1":   calc.net_at_tp1,
        "paper_mode":   paper_mode,
        "htf_timeframe": "4H",
    }

    # ── Stage 9: Send to Telegram ─────────────────────────────────
    message_id = await send_signal_to_telegram(_telegram_app.bot, chat_id, signal_data)

    # ── Stage 10: Save to database ────────────────────────────────
    async with factory() as session:
        sig = Signal(
            chat_id=chat_id,
            instrument=enriched.get("instrument"),
            segment=segment,
            direction=enriched.get("direction"),
            timeframe=enriched.get("timeframe"),
            session=enriched.get("session"),
            killzone_active=enriched.get("is_killzone", False),
            setup_type=enriched.get("setup_type"),
            score=score_result.score,
            grade=score_result.grade,
            entry=calc.entry,
            sl=calc.sl,
            tp1=calc.tp1,
            tp2=calc.tp2,
            tp3=calc.tp3,
            sl_points=calc.sl_points,
            sl_percent=calc.sl_percent,
            lots=calc.lots,
            charges=calc.charges,
            net_at_tp1=calc.net_at_tp1,
            confluences=enriched.get("confluences", {}),
            htf_bias=enriched.get("htf_bias"),
            liquidity_swept=enriched.get("liquidity_swept", False),
            fvg_present=enriched.get("fvg_present", False),
            volume_ratio=enriched.get("volume_ratio"),
            options_pcr=enriched.get("options_pcr"),
            options_oi_bias=enriched.get("options_oi_bias"),
            max_pain=enriched.get("max_pain"),
            gex=enriched.get("gex"),
            status="open",
            paper_mode=paper_mode,
            telegram_message_id=message_id,
        )
        session.add(sig)

        # Increment daily counter
        await increment_signals_today(session, user.user_id)
        await session.commit()

    logger.info(
        f"Signal pipeline complete: {enriched.get('instrument')} "
        f"{enriched.get('direction')} score={score_result.score} "
        f"grade={score_result.grade}"
    )
    return {
        "status": "sent",
        "instrument": enriched.get("instrument"),
        "direction": enriched.get("direction"),
        "score": score_result.score,
        "grade": score_result.grade,
    }


# ── Engine signal pipeline (live/evening/scanner → filter → DB → Telegram) ──

async def run_engine_signal_pipeline(signal_data: dict) -> None:
    """
    Run an engine-generated signal through filters, save to DB, and send.
    Engines already compute score/grade/entry/SL/TP so we skip webhook/scoring stages.
    """
    settings = get_settings()
    chat_id  = settings.telegram_chat_id
    factory  = get_session_factory()

    score = float(signal_data.get("score") or 0)
    grade = str(signal_data.get("grade") or "C")

    # ── Preference filter (segment enabled, score, daily limit, etc.) ─
    async with factory() as session:
        filter_result = await apply_filters(session, chat_id, signal_data, score=score, grade=grade)
        await session.commit()

    if not filter_result.passed:
        if filter_result.send_limit_warning:
            user = filter_result.user
            await send_daily_limit_warning(_telegram_app.bot, chat_id, user.max_signals_per_day if user else 5)
        if filter_result.reason and "consecutive_loss_protection" in filter_result.reason:
            if chat_id not in _consecutive_loss_warned:
                _consecutive_loss_warned.add(chat_id)
                await send_consecutive_loss_alert(_telegram_app.bot, chat_id, 3)
        logger.info(f"Engine signal filtered: {signal_data.get('instrument')} — {filter_result.reason}")
        return

    # ── Paper mode ────────────────────────────────────────────────
    async with factory() as session:
        user = filter_result.user or await get_or_create_user(session, chat_id)
        paper_mode = user.paper_mode
        user_id    = user.user_id
        await session.commit()

    signal_data["paper_mode"] = paper_mode

    # ── Send to Telegram ──────────────────────────────────────────
    message_id = await send_signal_to_telegram(_telegram_app.bot, chat_id, signal_data)

    # ── Save to DB ────────────────────────────────────────────────
    async with factory() as session:
        sig = Signal(
            chat_id=chat_id,
            instrument=signal_data.get("instrument"),
            segment=signal_data.get("segment"),
            direction=signal_data.get("direction"),
            timeframe=str(signal_data.get("timeframe", "15")),
            session=signal_data.get("session"),
            killzone_active=signal_data.get("is_killzone", False),
            setup_type=signal_data.get("setup_type"),
            score=int(score),
            grade=grade,
            entry=signal_data.get("entry"),
            sl=signal_data.get("sl"),
            tp1=signal_data.get("tp1"),
            tp2=signal_data.get("tp2"),
            tp3=signal_data.get("tp3"),
            sl_points=signal_data.get("sl_points"),
            sl_percent=signal_data.get("sl_percent"),
            lots=signal_data.get("lots", 1),
            charges=signal_data.get("charges"),
            net_at_tp1=signal_data.get("net_at_tp1"),
            confluences=signal_data.get("confluences", {}),
            htf_bias=signal_data.get("htf_bias"),
            liquidity_swept=signal_data.get("liquidity_swept", False),
            fvg_present=signal_data.get("fvg_present", False),
            volume_ratio=signal_data.get("volume_ratio"),
            options_pcr=signal_data.get("options_pcr"),
            options_oi_bias=signal_data.get("options_oi_bias"),
            max_pain=signal_data.get("max_pain"),
            gex=signal_data.get("gex"),
            status="open",
            paper_mode=paper_mode,
            telegram_message_id=message_id,
        )
        session.add(sig)
        await increment_signals_today(session, user_id)
        await session.commit()

    logger.info(
        f"Engine signal sent: {signal_data.get('instrument')} "
        f"{signal_data.get('direction')} score={score:.0f} grade={grade}"
    )


# ── Market scanner task ───────────────────────────────────────────

async def run_scanner_cycle() -> None:
    """Run one full market scan and pipe results through the engine signal pipeline."""
    try:
        from mcp_server.tools.market_scanner import get_scanner
        settings = get_settings()
        scanner = get_scanner()
        # Get enabled markets from main user preferences
        factory = get_session_factory()
        async with factory() as session:
            user = await get_or_create_user(session, settings.telegram_chat_id)
            enabled = user.markets_enabled or ["INDICES", "CRYPTO"]
            await session.commit()
        signals = await scanner.run_full_scan(enabled)
        for sig in signals:
            await run_engine_signal_pipeline(sig)
    except Exception as e:
        logger.error(f"Scanner cycle error: {e}", exc_info=True)


# ── Price tracker ─────────────────────────────────────────────────

async def check_open_signals_prices() -> None:
    """
    Check current prices for all open signals.
    If price hits SL or any TP level → close signal and send outcome notification.
    Runs every PRICE_CHECK_INTERVAL_SECONDS (default: 5 min).
    """
    settings = get_settings()
    factory = get_session_factory()

    try:
        async with factory() as session:
            result = await session.execute(
                select(Signal).where(Signal.status == "open")
            )
            open_signals = list(result.scalars().all())

        for sig in open_signals:
            if not sig.entry or not sig.sl:
                continue

            current_price = await fetch_current_price(sig.instrument or "", sig.segment or "")
            if not current_price:
                continue

            outcome = None
            exit_price = None

            if sig.direction == "LONG":
                if current_price <= sig.sl:
                    outcome = "SL"
                    exit_price = sig.sl
                elif sig.tp3 and current_price >= sig.tp3:
                    outcome = "TP3"
                    exit_price = sig.tp3
                elif sig.tp2 and current_price >= sig.tp2:
                    outcome = "TP2"
                    exit_price = sig.tp2
                elif sig.tp1 and current_price >= sig.tp1:
                    outcome = "TP1"
                    exit_price = sig.tp1
            else:  # SHORT
                if current_price >= sig.sl:
                    outcome = "SL"
                    exit_price = sig.sl
                elif sig.tp3 and current_price <= sig.tp3:
                    outcome = "TP3"
                    exit_price = sig.tp3
                elif sig.tp2 and current_price <= sig.tp2:
                    outcome = "TP2"
                    exit_price = sig.tp2
                elif sig.tp1 and current_price <= sig.tp1:
                    outcome = "TP1"
                    exit_price = sig.tp1

            if outcome and exit_price:
                # Calculate P&L
                pts = abs(exit_price - sig.entry)
                lot_size = 50  # Default
                is_win = outcome.startswith("TP")
                gross = pts * (sig.lots or 1) * lot_size
                net_pnl = (gross - (sig.charges or 0)) if is_win else -(pts * (sig.lots or 1) * lot_size) - (sig.charges or 0)

                # Update database
                async with factory() as session:
                    result = await session.execute(
                        select(Signal).where(Signal.id == sig.id)
                    )
                    db_sig = result.scalar_one_or_none()
                    if db_sig:
                        db_sig.status = "closed"
                        db_sig.outcome = outcome
                        db_sig.exit_price = exit_price
                        db_sig.actual_rr = pts / sig.sl_points if sig.sl_points else 0
                        db_sig.net_pnl = round(net_pnl, 2)
                        db_sig.closed_at = datetime.utcnow()
                        await session.commit()

                # Send outcome notification
                await send_outcome_to_telegram(
                    _telegram_app.bot, sig.chat_id or settings.telegram_chat_id,
                    sig, outcome, exit_price, round(net_pnl, 2),
                )
                logger.info(f"Signal closed: {sig.instrument} {outcome} exit={exit_price} pnl={net_pnl:.2f}")

    except Exception as e:
        logger.error(f"Price tracker error: {e}", exc_info=True)


# ── Daily summary ─────────────────────────────────────────────────

async def send_session_summary(session_name: str) -> None:
    """Send end-of-session daily summary to all users."""
    settings = get_settings()
    factory = get_session_factory()

    try:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        async with factory() as session:
            result = await session.execute(
                select(Signal).where(
                    Signal.chat_id == settings.telegram_chat_id,
                    Signal.created_at >= today_start,
                )
            )
            today_signals = list(result.scalars().all())

        if not today_signals:
            return

        summary = format_daily_summary(today_signals, session_name)
        await _telegram_app.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=summary,
            parse_mode="MarkdownV2",
        )
        logger.info(f"Daily {session_name} summary sent: {len(today_signals)} signals")

    except Exception as e:
        logger.error(f"Daily summary error: {e}", exc_info=True)


# ── Weekly analysis schedule ──────────────────────────────────────

async def run_scheduled_weekly_analysis() -> None:
    """Called by APScheduler Sunday 8 PM IST."""
    settings = get_settings()
    factory = get_session_factory()
    logger.info("Running scheduled weekly analysis...")
    async with factory() as session:
        from mcp_server.tools.performance import run_weekly_analysis
        await run_weekly_analysis(session, _telegram_app.bot, settings.telegram_chat_id)
        await session.commit()


# ── App lifespan ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown sequence."""
    global _telegram_app, _scheduler, _consecutive_loss_warned

    settings = get_settings()
    logger.info(f"SMC Trading Bot starting on port {settings.port}")
    logger.info(f"Environment: {settings.railway_environment}")
    logger.info(f"Paper mode: {settings.paper_mode}")

    # ── Database initialization ───────────────────────────────────
    await init_db()
    logger.info("Database ready")

    # ── Telegram bot setup ────────────────────────────────────────
    _telegram_app = build_telegram_app(settings.telegram_bot_token)
    await _telegram_app.initialize()
    await _telegram_app.start()
    await _telegram_app.updater.start_polling(drop_pending_updates=True, read_timeout=30, write_timeout=30)
    logger.info("Telegram bot polling started")

    # ── Scheduler setup ───────────────────────────────────────────
    _scheduler = AsyncIOScheduler(timezone=IST)

    # Price tracker: every 5 minutes
    _scheduler.add_job(
        check_open_signals_prices,
        "interval",
        seconds=PRICE_CHECK_INTERVAL_SECONDS,
        id="price_tracker",
        max_instances=1,
    )

    # India session summary: 3:45 PM IST daily
    india_h, india_m = map(int, DAILY_SUMMARY_INDIA_TIME.split(":"))
    _scheduler.add_job(
        lambda: asyncio.create_task(send_session_summary("Indian Market")),
        CronTrigger(hour=india_h, minute=india_m, timezone=IST),
        id="india_summary",
    )

    # Global session summary: 11:30 PM IST daily
    global_h, global_m = map(int, DAILY_SUMMARY_GLOBAL_TIME.split(":"))
    _scheduler.add_job(
        lambda: asyncio.create_task(send_session_summary("Global Markets")),
        CronTrigger(hour=global_h, minute=global_m, timezone=IST),
        id="global_summary",
    )

    # Weekly analysis: Sunday 8:00 PM IST
    weekly_h, weekly_m = map(int, WEEKLY_ANALYSIS_TIME.split(":"))
    _scheduler.add_job(
        run_scheduled_weekly_analysis,
        CronTrigger(
            day_of_week="sun",
            hour=weekly_h,
            minute=weekly_m,
            timezone=IST,
        ),
        id="weekly_analysis",
    )

    # Reset consecutive loss warning daily at midnight IST
    _scheduler.add_job(
        lambda: _consecutive_loss_warned.clear(),
        CronTrigger(hour=0, minute=1, timezone=IST),
        id="reset_loss_warning",
    )

    # Market scanner: every 30 minutes
    _scheduler.add_job(
        lambda: asyncio.create_task(run_scanner_cycle()),
        "interval",
        minutes=30,
        id="market_scanner",
        max_instances=1,
    )

    _scheduler.start()
    asyncio.get_event_loop().call_later(10, lambda: asyncio.create_task(start_live_engine()))
    asyncio.get_event_loop().call_later(15, lambda: asyncio.create_task(start_evening_engine()))
    logger.info("Scheduler started: price_tracker, india_summary, global_summary, weekly_analysis, market_scanner")

    yield  # ── Server running ───────────────────────────────────────

    # ── Shutdown ──────────────────────────────────────────────────
    logger.info("Shutting down...")
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    if _telegram_app:
        await _telegram_app.updater.stop()
        await _telegram_app.stop()
        await _telegram_app.shutdown()
    await close_db()
    logger.info("Shutdown complete")


# ── FastAPI application ───────────────────────────────────────────

app = FastAPI(
    title="SMC Trading Bot",
    description="Institutional-grade Smart Money Concepts signal system",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check():
    """Railway health check endpoint."""
    settings = get_settings()
    return {
        "status": "ok",
        "mode": "paper" if settings.paper_mode else "live",
        "environment": settings.railway_environment,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/webhook")
async def receive_webhook(request: Request):
    """
    TradingView webhook receiver.
    Validates secret, runs full pipeline, returns status.
    """
    # Parse JSON body
    try:
        raw_body = await request.json()
    except Exception as e:
        logger.warning(f"Invalid JSON in webhook: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        )

    # Quick secret check before expensive processing
    incoming_secret = raw_body.get("secret", "")
    settings = get_settings()
    if incoming_secret != settings.tv_webhook_secret:
        logger.warning(f"Webhook rejected: invalid secret from {request.client.host if request.client else 'unknown'}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )

    # Run full pipeline asynchronously (don't block the response)
    asyncio.create_task(run_signal_pipeline(raw_body))

    # Immediately return 200 to TradingView (avoids retry storms)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "received"},
    )


@app.get("/")
async def root():
    """Root endpoint."""
    return {"message": "SMC Trading Bot is running", "docs": "/docs"}


# ── Entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    settings = get_settings()
    port = settings.port

    uvicorn.run(
        "mcp_server.server:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=True,
        reload=False,
    )


async def start_live_engine():
    """Start live data engine - runs during market hours only."""
    try:
        from mcp_server.tools.live_data_engine import get_live_engine
        from mcp_server.tools.telegram_bot import send_signal_to_telegram
        settings = get_settings()
        engine = get_live_engine()
        async def on_signal(signal_data):
            try:
                await run_engine_signal_pipeline(signal_data)
            except Exception as e:
                logger.error(f"Signal send error: {e}")
        engine.set_signal_callback(on_signal)
        logger.info("Starting live data engine...")
        await engine.run()
    except Exception as e:
        logger.error(f"Live engine startup error: {e}", exc_info=True)


async def start_evening_engine():
    """Start evening session engine for XAUUSD + Forex."""
    try:
        from mcp_server.tools.evening_session_engine import get_evening_engine
        from mcp_server.tools.telegram_bot import send_signal_to_telegram
        settings = get_settings()
        engine = get_evening_engine()
        async def on_signal(signal_data):
            try:
                await run_engine_signal_pipeline(signal_data)
            except Exception as e:
                logger.error(f"Evening signal send error: {e}")
        engine.set_signal_callback(on_signal)
        logger.info("Starting evening session engine...")
        await engine.run()
    except Exception as e:
        logger.error(f"Evening engine startup error: {e}", exc_info=True)
