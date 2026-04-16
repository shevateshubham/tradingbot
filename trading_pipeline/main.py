"""
main.py — Pipeline Orchestrator

Starts:
  1. Health check HTTP server (for Railway)
  2. APScheduler — weekly review (Sun 8 PM IST) + auto-scan job
  3. Telegram bot (polling)

Two scanning modes run simultaneously:
  - Auto-scan: fires every N minutes (configurable), sends signals automatically
  - On-demand:  user sends /trade <MARKET> → ranked list → tap to get signal
"""

from __future__ import annotations

import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from apscheduler.triggers.cron import CronTrigger
import pytz

from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler

from utils.logger import get_logger
from utils.config_loader import load_config
from scanner.auto_scanner import get_scheduler, start_auto_scanner
from telegram_interface.command_handler import (
    start_command,
    help_command,
    markets_command,
    trade_command,
    status_command,
    stop_command,
    autoscan_command,
    button_callback,
)

logger     = get_logger("main")
IST        = pytz.timezone("Asia/Kolkata")
_START_TIME = datetime.now(timezone.utc).isoformat()


# ── Health check server ───────────────────────────────────────────────────────

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
        pass


def _start_health_server(port: int) -> None:
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Health server running on port {port}")


# ── Weekly review ─────────────────────────────────────────────────────────────

def _run_weekly_review() -> None:
    logger.info("Starting weekly review...")
    try:
        from weekly_review import run_weekly_review
        run_weekly_review()
    except Exception as e:
        logger.error(f"Weekly review failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    config = load_config()
    token  = os.environ.get("TELEGRAM_BOT_TOKEN") or \
             config.get("telegram", {}).get("bot_token", "")
    port   = int(os.environ.get("PORT", 8080))

    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set — exiting")
        sys.exit(1)

    # ── Health server ──────────────────────────────────────────────
    _start_health_server(port)

    # ── Shared APScheduler (used for both auto-scan + weekly review)
    scheduler = get_scheduler()

    # Weekly review: every Sunday 8 PM IST
    scheduler.add_job(
        _run_weekly_review,
        CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=IST),
        id="weekly_review",
        name="Weekly Review",
        replace_existing=True,
    )

    # Auto-scan: start immediately if enabled in config
    auto = config.get("auto_scan", {})
    if auto.get("enabled", False):
        interval = auto.get("interval_minutes", 15)
        start_auto_scanner(interval)
        logger.info(f"Auto-scan ENABLED — every {interval} minutes")
    else:
        scheduler.start()   # Still need scheduler for weekly review
        logger.info("Auto-scan DISABLED — use /autoscan on to enable")

    logger.info("Scheduler started — weekly review: Sundays 20:00 IST")

    # ── Telegram bot ───────────────────────────────────────────────
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start",    start_command))
    app.add_handler(CommandHandler("help",     help_command))
    app.add_handler(CommandHandler("markets",  markets_command))
    app.add_handler(CommandHandler("trade",    trade_command))
    app.add_handler(CommandHandler("status",   status_command))
    app.add_handler(CommandHandler("stop",     stop_command))
    app.add_handler(CommandHandler("autoscan", autoscan_command))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Telegram bot starting — send /start to begin")
    app.run_polling(poll_interval=1.0, timeout=30, drop_pending_updates=True)


if __name__ == "__main__":
    main()
