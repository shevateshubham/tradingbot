"""
scanner/auto_scanner.py — Background Auto-Scanner

Runs on a configurable interval, checks which markets are open,
runs Bots 1-5 for each open market, and sends qualifying signals directly.

Deduplication rules (smart — avoids missing quality trades):
  Key: symbol + direction only (simple and stable)
  Block only if: same key sent within dedup_window AND no meaningful upgrade.

  Always send (bypass dedup) when:
    1. WATCH → TRADE escalation        (setup became actionable)
    2. Grade upgrade  (B→A, A→A+)      (higher-quality setup now)
    3. Confidence jump ≥ 15 pts        (meaningfully stronger confluence)
    4. Dedup window has expired        (fresh signal period)

WATCH follow-up:
  When a WATCH signal is detected, a one-shot rescan fires for that
  specific symbol after `watch_followup_minutes` (default 5).
  This catches WATCH → TRADE upgrades without waiting for the next full cycle.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger

from utils.config_loader import load_config, save_config
from utils.logger import get_logger
from utils.market_hours import is_market_open, get_open_markets

logger = get_logger("auto_scanner")

DATA_DIR   = Path(__file__).parent.parent / "data"
DEDUP_FILE = DATA_DIR / "sent_signals.json"

GRADE_ORDER = {"C": 0, "B": 1, "A": 2, "A+": 3}

_scheduler: BackgroundScheduler | None = None
JOB_ID = "auto_scan"
_market_was_open: bool = True   # track open→close transition


# ── Deduplication ──────────────────────────────────────────────────────────────

def _dedup_key(sym: str, sym_data: dict) -> str:
    """
    Stable dedup key: symbol + direction only.
    Intentionally excludes setup_type and grade so that:
      - The key stays consistent as confluence builds up
      - Smart bypass rules decide whether to actually send
    """
    direction = sym_data.get("setup", {}).get("direction", "UNKNOWN")
    return f"{sym}_{direction}"


def _load_dedup() -> dict:
    if DEDUP_FILE.exists():
        try:
            with open(DEDUP_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_dedup(store: dict) -> None:
    DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DEDUP_FILE, "w") as f:
        json.dump(store, f, indent=2)


def _prune_dedup(store: dict, max_age_hours: int = 24) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    return {
        k: v for k, v in store.items()
        if datetime.fromisoformat(v["sent_at"]) > cutoff
    }


def _should_send(sym: str, sym_data: dict, window_minutes: int) -> tuple[bool, str]:
    """
    Decide whether to send this signal, applying smart bypass rules.
    Returns (should_send: bool, reason: str).
    """
    key      = _dedup_key(sym, sym_data)
    store    = _load_dedup()
    decision = sym_data.get("decision", {})

    new_action = decision.get("action", "NO_TRADE")
    new_conf   = decision.get("confidence_score", 0.0)
    new_grade  = decision.get("grade", "C")

    # Never seen before
    if key not in store:
        return True, "first_signal"

    entry     = store[key]
    sent_at   = datetime.fromisoformat(entry["sent_at"])
    old_action = entry.get("action", "NO_TRADE")
    old_conf   = entry.get("confidence", 0.0)
    old_grade  = entry.get("grade", "C")

    # Window expired → fresh slate
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    if sent_at < cutoff:
        return True, "window_expired"

    # ── Smart bypass rules (within the dedup window) ──────────────────────────

    # Rule 1: WATCH → TRADE escalation (the setup became actionable)
    if old_action == "WATCH" and new_action == "TRADE":
        return True, "watch_to_trade_escalation"

    # Rule 2: Grade upgrade (quality improved)
    if GRADE_ORDER.get(new_grade, 0) > GRADE_ORDER.get(old_grade, 0):
        return True, f"grade_upgrade_{old_grade}_to_{new_grade}"

    # Rule 3: Confidence jump ≥ 15 points (meaningfully stronger confluence)
    if new_conf - old_conf >= 15.0:
        return True, f"confidence_jump_{old_conf:.0f}_to_{new_conf:.0f}"

    # Duplicate — block
    return False, f"duplicate_within_{window_minutes}min"


def _mark_sent(sym: str, sym_data: dict) -> None:
    """Record that this signal was sent."""
    key      = _dedup_key(sym, sym_data)
    decision = sym_data.get("decision", {})
    store    = _prune_dedup(_load_dedup())
    store[key] = {
        "sent_at":   datetime.now(timezone.utc).isoformat(),
        "action":    decision.get("action", "NO_TRADE"),
        "grade":     decision.get("grade", "C"),
        "confidence": decision.get("confidence_score", 0.0),
        "setup_type": sym_data.get("setup", {}).get("setup_type", ""),
    }
    _save_dedup(store)


# ── Pipeline runners ───────────────────────────────────────────────────────────

def _get_trade_type_config(trade_type: str, config: dict) -> dict:
    return config.get("trade_types", {}).get(trade_type, {
        "timeframes": ["1m", "5m", "15m"],
        "htf_tf": "15m", "ltf_tf": "1m",
        "min_rr": 2.0, "scan_interval_minutes": 15,
    })


def _run_pipeline_for_market(market: str, trade_type: str | None = None) -> dict:
    """Run Bots 1-5 for an entire market. Returns pipeline dict."""
    from bots.bot1_data_collector import DataCollectorBot
    from bots.bot2_context        import ContextBot
    from bots.bot3_setup          import SetupBot
    from bots.bot4_trigger        import TriggerBot
    from bots.bot5_decision       import DecisionBot

    config    = load_config()
    tt        = trade_type or config.get("auto_scan", {}).get("trade_type", "intraday")
    tt_config = _get_trade_type_config(tt, config)
    run_id    = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    pipeline = {
        "run_id":            run_id,
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "selected_market":   market,
        "trade_type":        tt,
        "trade_type_config": tt_config,
    }
    pipeline = DataCollectorBot().run(pipeline)
    pipeline = ContextBot().run(pipeline)
    pipeline = SetupBot().run(pipeline)
    pipeline = TriggerBot().run(pipeline)
    pipeline = DecisionBot().run(pipeline)
    return pipeline


def _run_pipeline_for_symbol(sym: str, market: str) -> dict | None:
    """
    Run Bots 1-5 for a SINGLE symbol only.
    Used for WATCH follow-up scans — avoids re-fetching the whole market.
    Returns a mini pipeline dict with just this symbol, or None on failure.
    """
    from bots.bot1_data_collector import DataCollectorBot
    from bots.bot2_context        import ContextBot
    from bots.bot3_setup          import SetupBot
    from bots.bot4_trigger        import TriggerBot
    from bots.bot5_decision       import DecisionBot

    config   = load_config()
    run_id   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + f"_fu_{sym}"

    # Temporarily override symbols in the pipeline dict
    pipeline = {
        "run_id":          run_id,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "selected_market": market,
        "_single_symbol":  sym,   # flag for DataCollectorBot to fetch only this
    }

    try:
        # We monkey-patch the config temporarily so Bot 1 fetches only `sym`
        original_syms = config.get("symbols", {}).get(market, [])
        config["symbols"] = dict(config.get("symbols", {}))
        config["symbols"][market] = [sym]

        from utils.config_loader import _CONFIG
        import utils.config_loader as cl
        old_config = cl._CONFIG.copy()
        cl._CONFIG = config

        pipeline = DataCollectorBot().run(pipeline)
        pipeline = ContextBot().run(pipeline)
        pipeline = SetupBot().run(pipeline)
        pipeline = TriggerBot().run(pipeline)
        pipeline = DecisionBot().run(pipeline)

        cl._CONFIG = old_config  # Restore
        return pipeline
    except Exception as e:
        logger.error(f"Follow-up pipeline error for {sym}: {e}")
        return None


# ── Signal processing ──────────────────────────────────────────────────────────

def _process_symbol(
    sym: str,
    sym_data: dict,
    run_id: str,
    config: dict,
    min_conf: float,
    dedup_window: int,
    send_watches: bool,
    followup_minutes: int,
) -> int:
    """
    Evaluate and send signal for one symbol. Returns 1 if sent, 0 otherwise.
    Also schedules a follow-up scan if action is WATCH.
    """
    from bots.bot6_notification import send_signal_for_symbol

    if sym_data.get("fetch_status") != "ok":
        return 0

    decision = sym_data.get("decision", {})
    action   = decision.get("action", "NO_TRADE")
    conf     = decision.get("confidence_score", 0.0)
    market   = sym_data.get("market", "")

    # Qualify: must meet minimum confidence
    qualifies = (action == "TRADE" and conf >= min_conf) or \
                (action == "WATCH" and send_watches and conf >= min_conf * 0.80)

    if not qualifies:
        logger.info(f"  {sym}: {action} {conf:.0f}% — below threshold (min={min_conf}), skipping")
        return 0

    # Smart dedup check
    send, reason = _should_send(sym, sym_data, dedup_window)

    if not send:
        logger.info(f"  {sym}: {action} {conf:.0f}% — suppressed ({reason})")
        # Even if suppressed as TRADE, still schedule a follow-up for WATCHes
        # that were previously sent — they may have upgraded since
        return 0

    # Send the signal
    logger.info(f"  {sym}: SENDING {action} {conf:.0f}% [{decision.get('grade')}] — {reason}")
    send_signal_for_symbol(
        sym            = sym,
        sym_data       = sym_data,
        config         = config,
        run_id         = run_id,
        target_chat_id = None,  # Default TELEGRAM_CHAT_ID
    )
    _mark_sent(sym, sym_data)

    # Schedule follow-up if this was a WATCH (to catch upgrade to TRADE quickly)
    if action == "WATCH":
        _schedule_followup(sym, market, followup_minutes)
        logger.info(f"  {sym}: WATCH detected — follow-up scan in {followup_minutes}min")

    return 1


def _schedule_followup(sym: str, market: str, followup_minutes: int) -> None:
    """
    Schedule a one-shot follow-up scan for a single symbol.
    Fires after `followup_minutes` to catch WATCH → TRADE upgrade.
    Replaces any existing follow-up for this symbol (no stacking).
    """
    sched    = get_scheduler()
    job_id   = f"followup_{sym}"
    run_time = datetime.now(timezone.utc) + timedelta(minutes=followup_minutes)

    if sched.get_job(job_id):
        sched.remove_job(job_id)

    sched.add_job(
        _followup_job,
        trigger  = DateTrigger(run_date=run_time),
        args     = [sym, market],
        id       = job_id,
        name     = f"Follow-up: {sym}",
        replace_existing = True,
    )
    logger.info(f"Follow-up scheduled: {sym} @ {run_time.strftime('%H:%M:%S UTC')}")


def _followup_job(sym: str, market: str) -> None:
    """
    One-shot follow-up scan for a single symbol.
    Runs the full pipeline for just this symbol and sends if upgraded.
    """
    logger.info(f"Follow-up scan: {sym} ({market})")
    config = load_config()
    auto   = config.get("auto_scan", {})

    if not auto.get("enabled", False):
        return

    pipeline = _run_pipeline_for_symbol(sym, market)
    if not pipeline:
        return

    sym_data = pipeline.get("symbols", {}).get(sym)
    if not sym_data:
        return

    decision = sym_data.get("decision", {})
    action   = decision.get("action", "NO_TRADE")
    conf     = decision.get("confidence_score", 0.0)

    logger.info(f"  Follow-up result: {sym} → {action} {conf:.0f}%")

    _process_symbol(
        sym              = sym,
        sym_data         = sym_data,
        run_id           = pipeline.get("run_id", "followup"),
        config           = config,
        min_conf         = auto.get("min_confidence_auto", 70),
        dedup_window     = auto.get("dedup_window_minutes", 60),
        send_watches     = auto.get("send_watches", False),
        followup_minutes = auto.get("watch_followup_minutes", 5),
    )


# ── Main auto-scan job ─────────────────────────────────────────────────────────

def _notify_market_closed(market: str, config: dict) -> None:
    """Send a Telegram message that the market has closed and scanning has stopped."""
    import os, httpx
    token   = os.environ.get("TELEGRAM_BOT_TOKEN") or config.get("telegram", {}).get("bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or config.get("telegram", {}).get("chat_id", "")
    if not token or not chat_id:
        return
    text = (
        f"🔒 <b>{market} market is now closed.</b>\n\n"
        f"Continuous scan has stopped automatically.\n"
        f"Tap /start when the market reopens to begin scanning again."
    )
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10.0,
        )
    except Exception as e:
        logger.warning(f"Could not send market-closed notification: {e}")


def run_auto_scan() -> None:
    """
    Scheduled job — runs every N minutes.
    Scans the configured market and sends A/A+ TRADE signals.
    Auto-stops when the market closes and notifies the user.
    """
    global _market_was_open
    config = load_config()
    auto   = config.get("auto_scan", {})

    if not auto.get("enabled", False):
        return

    min_conf         = auto.get("min_confidence_auto", 75)
    dedup_window     = auto.get("dedup_window_minutes", 60)
    send_watches     = auto.get("send_watches", False)
    followup_minutes = auto.get("watch_followup_minutes", 5)
    trade_type       = auto.get("trade_type", "intraday")
    scan_market      = auto.get("market", "")

    open_markets = get_open_markets(config)

    # Determine target markets
    if scan_market and scan_market != "ALL":
        market_is_open = scan_market in open_markets or scan_market == "CRYPTO"
        target_markets = [scan_market] if market_is_open else []
    else:
        target_markets = [m for m in auto.get("markets", config.get("markets", [])) if m in open_markets]

    # Detect open → closed transition — stop scanner and notify
    if not target_markets and _market_was_open and scan_market and scan_market != "CRYPTO":
        logger.info(f"Auto-scan: {scan_market} just closed — stopping scanner")
        _market_was_open = False
        stop_auto_scanner()
        auto["enabled"] = False
        config["auto_scan"] = auto
        save_config(config)
        _notify_market_closed(scan_market, config)
        return

    if not target_markets:
        logger.info(f"Auto-scan: {scan_market} is closed — skipping this cycle")
        return

    _market_was_open = True
    logger.info(f"Auto-scan — market: {target_markets} | type: {trade_type}")
    total_sent = 0

    for market in target_markets:
        try:
            pipeline = _run_pipeline_for_market(market, trade_type)
            run_id   = pipeline.get("run_id", "unknown")

            for sym, sym_data in pipeline.get("symbols", {}).items():
                total_sent += _process_symbol(
                    sym              = sym,
                    sym_data         = sym_data,
                    run_id           = run_id,
                    config           = config,
                    min_conf         = min_conf,
                    dedup_window     = dedup_window,
                    send_watches     = send_watches,
                    followup_minutes = followup_minutes,
                )
        except Exception as e:
            logger.error(f"Auto-scan error for {market}: {e}")

    logger.info(f"Auto-scan complete — {total_sent} signal(s) sent")


# ── Scheduler management ───────────────────────────────────────────────────────

def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="UTC")
    return _scheduler


def start_auto_scanner(interval_minutes: int) -> None:
    """Start or reschedule the auto-scan job."""
    sched = get_scheduler()

    if not sched.running:
        sched.start()
        logger.info("APScheduler started")

    if sched.get_job(JOB_ID):
        sched.remove_job(JOB_ID)

    sched.add_job(
        run_auto_scan,
        trigger          = IntervalTrigger(minutes=interval_minutes),
        id               = JOB_ID,
        name             = f"Auto-scan every {interval_minutes}m",
        replace_existing = True,
        # First run after one interval — button_callback already ran an initial scan
    )
    logger.info(f"Auto-scan scheduled: every {interval_minutes} minutes (first run in {interval_minutes}m)")


def stop_auto_scanner() -> None:
    """Remove the periodic auto-scan job (scheduler stays alive)."""
    sched = get_scheduler()
    if sched.get_job(JOB_ID):
        sched.remove_job(JOB_ID)
        logger.info("Auto-scan job removed")


def update_interval(interval_minutes: int, config: dict) -> dict:
    """Change interval, persist to config, reschedule."""
    auto = config.get("auto_scan", {})
    auto["interval_minutes"] = interval_minutes
    config["auto_scan"] = auto
    save_config(config)
    if auto.get("enabled", False):
        start_auto_scanner(interval_minutes)
    return config


def set_enabled(enabled: bool) -> None:
    """Enable or disable auto-scan, persist to config."""
    config = load_config()
    auto   = config.get("auto_scan", {})
    auto["enabled"] = enabled
    config["auto_scan"] = auto
    save_config(config)
    if enabled:
        start_auto_scanner(auto.get("interval_minutes", 15))
    else:
        stop_auto_scanner()


def get_status() -> dict:
    """Return current status dict for /autoscan command."""
    config = load_config()
    auto   = config.get("auto_scan", {})
    sched  = get_scheduler()
    job    = sched.get_job(JOB_ID) if sched.running else None

    # Count active follow-up jobs
    followup_count = sum(
        1 for j in (sched.get_jobs() if sched.running else [])
        if j.id.startswith("followup_")
    )

    return {
        "enabled":          auto.get("enabled", False),
        "interval_minutes": auto.get("interval_minutes", 15),
        "markets":          auto.get("markets", config.get("markets", [])),
        "min_confidence":   auto.get("min_confidence_auto", 70),
        "dedup_window":     auto.get("dedup_window_minutes", 60),
        "send_watches":     auto.get("send_watches", False),
        "followup_minutes": auto.get("watch_followup_minutes", 5),
        "next_run":         job.next_run_time.strftime("%H:%M:%S UTC") if (job and job.next_run_time) else "—",
        "active_followups": followup_count,
        "scheduler_alive":  sched.running if sched else False,
    }
