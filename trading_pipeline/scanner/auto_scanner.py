"""
scanner/auto_scanner.py — Background Auto-Scanner

Runs on a configurable interval, checks which markets are currently open,
runs the full pipeline (Bots 1-5) for each open market, and sends TRADE/WATCH
signals directly to Telegram — no user interaction needed.

Deduplication: tracks recently sent signals so the same setup is not
re-alerted within the dedup_window_minutes (default 60 min).

Controlled via /autoscan command:
  /autoscan         → show status
  /autoscan on      → enable
  /autoscan off     → disable
  /autoscan 5m      → set interval to 5 minutes
  /autoscan 15m     → set interval to 15 minutes
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from utils.config_loader import load_config, save_config
from utils.logger import get_logger
from utils.market_hours import is_market_open, get_open_markets

logger = get_logger("auto_scanner")

DATA_DIR    = Path(__file__).parent.parent / "data"
DEDUP_FILE  = DATA_DIR / "sent_signals.json"

# Single shared scheduler instance (managed by main.py)
_scheduler: BackgroundScheduler | None = None
JOB_ID = "auto_scan"


# ── Deduplication ──────────────────────────────────────────────────────────────

def _dedup_key(sym: str, sym_data: dict) -> str:
    """Unique key for a specific setup on a symbol."""
    direction  = sym_data.get("setup", {}).get("direction", "?")
    setup_type = sym_data.get("setup", {}).get("setup_type", "?")
    grade      = sym_data.get("decision", {}).get("grade", "?")
    return f"{sym}_{direction}_{setup_type}_{grade}"


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


def _is_duplicate(sym: str, sym_data: dict, window_minutes: int) -> bool:
    """Return True if this exact setup was already sent within the dedup window."""
    key   = _dedup_key(sym, sym_data)
    store = _load_dedup()
    if key not in store:
        return False
    sent_at  = datetime.fromisoformat(store[key])
    cutoff   = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    return sent_at > cutoff


def _mark_sent(sym: str, sym_data: dict) -> None:
    """Record that this setup was just sent."""
    key   = _dedup_key(sym, sym_data)
    store = _load_dedup()

    # Prune entries older than 24 hours to keep the file small
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    store  = {
        k: v for k, v in store.items()
        if datetime.fromisoformat(v) > cutoff
    }
    store[key] = datetime.now(timezone.utc).isoformat()
    _save_dedup(store)


def _clear_dedup_for_symbol(sym: str) -> None:
    """Remove all dedup entries for a symbol (force re-alert next scan)."""
    store = _load_dedup()
    store = {k: v for k, v in store.items() if not k.startswith(sym + "_")}
    _save_dedup(store)


# ── Pipeline runner ────────────────────────────────────────────────────────────

def _run_pipeline_for_market(market: str) -> dict:
    """Run Bots 1-5 for a single market. Returns enriched pipeline dict."""
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


# ── Auto-scan job ──────────────────────────────────────────────────────────────

def run_auto_scan() -> None:
    """
    The scheduled job — called every N minutes by APScheduler.
    Scans all open markets, sends qualifying signals directly.
    """
    config = load_config()
    auto   = config.get("auto_scan", {})

    if not auto.get("enabled", False):
        return  # Auto-scan disabled — do nothing

    min_conf       = auto.get("min_confidence_auto", config.get("scoring", {}).get("min_confidence", 70))
    dedup_window   = auto.get("dedup_window_minutes", 60)
    send_watches   = auto.get("send_watches", False)

    open_markets = get_open_markets(config)
    scanned_markets = auto.get("markets", config.get("markets", []))
    targets = [m for m in scanned_markets if m in open_markets]

    if not targets:
        logger.debug("Auto-scan: no open markets right now")
        return

    logger.info(f"Auto-scan started — open markets: {targets}")
    total_sent = 0

    for market in targets:
        try:
            pipeline = _run_pipeline_for_market(market)
            run_id   = pipeline.get("run_id", "unknown")

            from bots.bot6_notification import send_signal_for_symbol

            for sym, sym_data in pipeline.get("symbols", {}).items():
                if sym_data.get("fetch_status") != "ok":
                    continue

                decision = sym_data.get("decision", {})
                action   = decision.get("action", "NO_TRADE")
                conf     = decision.get("confidence_score", 0)

                should_send = (
                    (action == "TRADE" and conf >= min_conf) or
                    (action == "WATCH" and send_watches)
                )

                if not should_send:
                    continue

                if _is_duplicate(sym, sym_data, dedup_window):
                    logger.info(f"  {sym}: skip (duplicate within {dedup_window}min)")
                    continue

                # Send directly — no user tap needed
                send_signal_for_symbol(
                    sym            = sym,
                    sym_data       = sym_data,
                    config         = config,
                    run_id         = run_id,
                    target_chat_id = None,  # uses TELEGRAM_CHAT_ID env var
                )
                _mark_sent(sym, sym_data)
                total_sent += 1

        except Exception as e:
            logger.error(f"Auto-scan error for {market}: {e}")

    logger.info(f"Auto-scan complete — {total_sent} signal(s) sent across {len(targets)} markets")


# ── Scheduler management ───────────────────────────────────────────────────────

def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="UTC")
    return _scheduler


def start_auto_scanner(interval_minutes: int) -> None:
    """Start or restart the auto-scan job at the given interval."""
    sched = get_scheduler()

    if not sched.running:
        sched.start()
        logger.info("APScheduler started for auto-scan")

    # Remove existing job if present
    if sched.get_job(JOB_ID):
        sched.remove_job(JOB_ID)

    sched.add_job(
        run_auto_scan,
        trigger  = IntervalTrigger(minutes=interval_minutes),
        id       = JOB_ID,
        name     = f"Auto-scan every {interval_minutes}m",
        replace_existing = True,
        next_run_time = datetime.now(timezone.utc),  # Run immediately on start
    )
    logger.info(f"Auto-scan scheduled: every {interval_minutes} minutes")


def stop_auto_scanner() -> None:
    """Pause the auto-scan job (scheduler stays alive for weekly review)."""
    sched = get_scheduler()
    if sched.get_job(JOB_ID):
        sched.remove_job(JOB_ID)
        logger.info("Auto-scan job removed")


def update_interval(interval_minutes: int, config: dict) -> dict:
    """Change the scan interval: update config + reschedule the job."""
    auto = config.get("auto_scan", {})
    auto["interval_minutes"] = interval_minutes
    config["auto_scan"] = auto
    save_config(config)

    if auto.get("enabled", False):
        start_auto_scanner(interval_minutes)
    return config


def get_status() -> dict:
    """Return current auto-scanner status for /autoscan command."""
    config  = load_config()
    auto    = config.get("auto_scan", {})
    sched   = get_scheduler()
    job     = sched.get_job(JOB_ID) if sched.running else None

    next_run = None
    if job and job.next_run_time:
        next_run = job.next_run_time.strftime("%H:%M:%S UTC")

    return {
        "enabled":          auto.get("enabled", False),
        "interval_minutes": auto.get("interval_minutes", 15),
        "markets":          auto.get("markets", config.get("markets", [])),
        "min_confidence":   auto.get("min_confidence_auto", 70),
        "dedup_window":     auto.get("dedup_window_minutes", 60),
        "send_watches":     auto.get("send_watches", False),
        "next_run":         next_run,
        "scheduler_alive":  sched.running if sched else False,
    }


def set_enabled(enabled: bool) -> None:
    """Enable or disable auto-scan and persist to config."""
    config = load_config()
    auto   = config.get("auto_scan", {})
    auto["enabled"] = enabled
    config["auto_scan"] = auto
    save_config(config)

    if enabled:
        start_auto_scanner(auto.get("interval_minutes", 15))
    else:
        stop_auto_scanner()
