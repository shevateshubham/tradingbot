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

from telegram.error import Conflict, NetworkError, TimedOut
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
    port   = int(os.environ.get("PORT", 8080))

    # ── Health server — must start first so Railway healthcheck passes ──
    _start_health_server(port)

    token = os.environ.get("TELEGRAM_BOT_TOKEN") or \
            config.get("telegram", {}).get("bot_token", "")

    if not token or token == "SET_ME":
        logger.error("TELEGRAM_BOT_TOKEN not configured — bot idle, health endpoint alive")
        import time
        while True:
            time.sleep(60)

    # ── Always start with scanning OFF — user picks market/style manually ──
    from utils.config_loader import save_config
    auto = config.get("auto_scan", {})
    if auto.get("enabled", False):
        auto["enabled"] = False
        config["auto_scan"] = auto
        save_config(config)
        logger.info("Startup: scanner reset to OFF — waiting for user selection")

    # ── Shared APScheduler (weekly review only on startup) ──
    scheduler = get_scheduler()

    # Weekly review: every Sunday 8 PM IST
    scheduler.add_job(
        _run_weekly_review,
        CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=IST),
        id="weekly_review",
        name="Weekly Review",
        replace_existing=True,
    )

    # Scheduler runs for weekly review only — scanning starts when user selects market
    scheduler.start()
    logger.info("Scheduler started (weekly review only) — all markets stopped at startup")

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

    # ── Error handler — prevent Conflict/network errors from crashing the bot ──
    async def error_handler(update, context):
        err = context.error
        if isinstance(err, Conflict):
            # Another instance briefly running during redeploy — harmless, will resolve
            logger.warning("Telegram Conflict: another instance detected, retrying...")
            return
        if isinstance(err, (NetworkError, TimedOut)):
            logger.warning(f"Telegram network error (will retry): {err}")
            return
        logger.error(f"Unhandled Telegram error: {err}", exc_info=err)

    app.add_error_handler(error_handler)

    logger.info("Telegram bot starting — send /start to begin")
    app.run_polling(poll_interval=2.0, timeout=30, drop_pending_updates=True)


if __name__ == "__main__":
    main()
