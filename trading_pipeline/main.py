"""
main.py — Pipeline Orchestrator
Starts the Telegram bot (polling) + APScheduler (weekly review).
A lightweight HTTP health endpoint runs in a background thread for Railway.

Architecture:
- On-demand: User sends /trade <MARKET> → pipeline runs for that market only
- No continuous 24/7 scanning of all symbols (saves API calls + hosting cost)
- Scheduler only runs: weekly review (Sun 8 PM IST)
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
from pathlib import Path

# Add trading_pipeline to Python path so relative imports work when
# running as: python trading_pipeline/main.py
sys.path.insert(0, str(Path(__file__).parent))

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler

from utils.logger import get_logger
from utils.config_loader import load_config
from telegram_interface.command_handler import (
    start_command,
    help_command,
    markets_command,
    trade_command,
    status_command,
    stop_command,
    button_callback,
)

logger = get_logger("main")
IST = pytz.timezone("Asia/Kolkata")

# ── Health check server ───────────────────────────────────────────────────────

_START_TIME = datetime.now(timezone.utc).isoformat()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/health", "/"):
            body = (
                f'{{"status":"ok","uptime_since":"{_START_TIME}",'
                f'"time":"{datetime.now(timezone.utc).isoformat()}"}}'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # Suppress HTTP access log noise


def _start_health_server(port: int) -> None:
    """Start a tiny HTTP health server in a daemon thread."""
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health server running on port {port}")


# ── Weekly review scheduler ───────────────────────────────────────────────────

def _run_weekly_review() -> None:
    """Called by APScheduler every Sunday 8 PM IST."""
    logger.info("Starting weekly review...")
    try:
        from weekly_review import run_weekly_review
        run_weekly_review()
    except Exception as e:
        logger.error(f"Weekly review failed: {e}")


# ── Main entry point ──────────────────────────────────────────────────────────

def main() -> None:
    config = load_config()

    # ── Read env vars ──────────────────────────────────────────────
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or \
            config.get("telegram", {}).get("bot_token", "")
    port  = int(os.environ.get("PORT", 8080))

    if not token:
        logger.error(
            "TELEGRAM_BOT_TOKEN not set. "
            "Set it as an environment variable or in config.json."
        )
        sys.exit(1)

    # ── Health check server (for Railway) ──────────────────────────
    _start_health_server(port)

    # ── APScheduler: weekly review ─────────────────────────────────
    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(
        _run_weekly_review,
        CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=IST),
        id="weekly_review",
        name="Weekly Review",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started — weekly review: Sundays 20:00 IST")

    # ── Telegram bot (polling) ─────────────────────────────────────
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start",   start_command))
    app.add_handler(CommandHandler("help",    help_command))
    app.add_handler(CommandHandler("markets", markets_command))
    app.add_handler(CommandHandler("trade",   trade_command))
    app.add_handler(CommandHandler("status",  status_command))
    app.add_handler(CommandHandler("stop",    stop_command))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Telegram bot starting (polling mode)...")
    logger.info("Send /start to your bot to begin.")

    # run_polling blocks until the process is killed
    app.run_polling(
        poll_interval=1.0,
        timeout=30,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
