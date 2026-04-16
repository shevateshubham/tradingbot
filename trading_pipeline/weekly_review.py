"""
weekly_review.py — Weekly Learning & Refinement System
Analyzes trade outcomes from data/trades/*.json, computes performance stats,
adjusts scoring thresholds in config.json, and sends a Telegram summary.

Runs automatically via APScheduler (Sunday 8 PM IST).
Can also be run manually: python weekly_review.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from utils.config_loader import load_config, save_config
from utils.logger import get_logger

logger = get_logger("weekly_review")

TRADES_DIR = Path(__file__).parent / "data" / "trades"
TELEGRAM_API = "https://api.telegram.org"


# ── Data Loading ──────────────────────────────────────────────────────────────

def load_trade_history(days: int = 30) -> list[dict]:
    """Load all trades from the past N days."""
    trades = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    for filepath in sorted(TRADES_DIR.glob("*.json")):
        try:
            date_str = filepath.stem  # "YYYY-MM-DD"
            file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if file_date < cutoff:
                continue
            with open(filepath) as f:
                day_trades = json.load(f)
            trades.extend(day_trades)
        except Exception as e:
            logger.warning(f"Could not load {filepath}: {e}")

    return trades


# ── Statistics ────────────────────────────────────────────────────────────────

def compute_stats(trades: list[dict]) -> dict:
    """
    Compute win rate, avg RR, drawdown, profit factor — overall and per market.
    Only considers closed trades (outcome != None).
    """
    closed = [t for t in trades if t.get("outcome") is not None]
    total  = len(closed)

    if total == 0:
        return {"total": 0, "insufficient_data": True}

    wins   = [t for t in closed if t.get("outcome") in ("TP1", "TP2", "TP3")]
    losses = [t for t in closed if t.get("outcome") == "SL"]

    win_rate = len(wins) / total if total > 0 else 0.0

    rr_values = [t.get("actual_rr", 0) for t in closed if t.get("actual_rr") is not None]
    avg_rr    = sum(rr_values) / len(rr_values) if rr_values else 0.0

    gross_profit = sum(r for r in rr_values if r > 0)
    gross_loss   = abs(sum(r for r in rr_values if r < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max consecutive losses
    max_dd_streak = 0
    current_streak = 0
    for t in closed:
        if t.get("outcome") == "SL":
            current_streak += 1
            max_dd_streak = max(max_dd_streak, current_streak)
        else:
            current_streak = 0

    # Per-market breakdown
    by_market: dict[str, dict] = {}
    for t in closed:
        market = t.get("market", "UNKNOWN")
        if market not in by_market:
            by_market[market] = {"total": 0, "wins": 0, "losses": 0, "rr_values": []}
        by_market[market]["total"] += 1
        if t.get("outcome") in ("TP1", "TP2", "TP3"):
            by_market[market]["wins"] += 1
        elif t.get("outcome") == "SL":
            by_market[market]["losses"] += 1
        if t.get("actual_rr") is not None:
            by_market[market]["rr_values"].append(t["actual_rr"])

    market_stats = {}
    for market, data in by_market.items():
        n = data["total"]
        w = data["wins"]
        market_stats[market] = {
            "total":       n,
            "wins":        w,
            "losses":      data["losses"],
            "win_rate":    w / n if n > 0 else 0.0,
            "avg_rr":      sum(data["rr_values"]) / len(data["rr_values"]) if data["rr_values"] else 0.0,
        }

    # Per-symbol breakdown
    by_symbol: dict[str, dict] = {}
    for t in closed:
        sym = t.get("symbol", "UNKNOWN")
        if sym not in by_symbol:
            by_symbol[sym] = {"total": 0, "wins": 0}
        by_symbol[sym]["total"] += 1
        if t.get("outcome") in ("TP1", "TP2", "TP3"):
            by_symbol[sym]["wins"] += 1

    return {
        "total":           total,
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        round(win_rate, 4),
        "avg_rr":          round(avg_rr, 4),
        "profit_factor":   round(profit_factor, 4),
        "max_drawdown_streak": max_dd_streak,
        "by_market":       market_stats,
        "by_symbol":       {
            sym: {"total": d["total"], "wins": d["wins"],
                  "win_rate": d["wins"] / d["total"] if d["total"] > 0 else 0.0}
            for sym, d in by_symbol.items()
        },
    }


# ── Adaptive Adjustments ──────────────────────────────────────────────────────

def identify_underperformers(stats: dict, min_trades: int = 5, threshold: float = 0.25) -> list[str]:
    """
    Return symbols with insufficient win rate (candidates for disabling).
    Only considers symbols with at least `min_trades` closed trades.
    """
    underperformers = []
    for sym, data in stats.get("by_symbol", {}).items():
        if data["total"] >= min_trades and data["win_rate"] < threshold:
            underperformers.append(sym)
    return underperformers


def adjust_scoring_thresholds(stats: dict, config: dict) -> tuple[dict, list[str]]:
    """
    Apply heuristic rules to adjust config scoring thresholds.
    Returns (updated_config, list_of_changes_made).
    """
    changes = []
    scoring = config.get("scoring", {})
    current_min = scoring.get("min_confidence", 70)

    win_rate = stats.get("win_rate", 0)

    if win_rate < 0.35:
        new_min = min(85, current_min + 5)
        if new_min != current_min:
            scoring["min_confidence"] = new_min
            changes.append(f"min_confidence: {current_min} → {new_min} (win rate {win_rate:.0%} < 35%)")

    elif win_rate > 0.65:
        new_min = max(55, current_min - 3)
        if new_min != current_min:
            scoring["min_confidence"] = new_min
            changes.append(f"min_confidence: {current_min} → {new_min} (win rate {win_rate:.0%} > 65%)")

    config["scoring"] = scoring

    # Disable underperformers
    underperformers = identify_underperformers(stats)
    existing_disabled = set(config.get("disabled_symbols", []))
    new_disabled = [s for s in underperformers if s not in existing_disabled]
    if new_disabled:
        config["disabled_symbols"] = list(existing_disabled) + new_disabled
        changes.append(f"Disabled symbols: {', '.join(new_disabled)}")

    return config, changes


# ── Telegram summary ──────────────────────────────────────────────────────────

def _send_telegram_summary(text: str) -> None:
    """Send weekly summary to Telegram."""
    import httpx
    config  = load_config()
    token   = os.environ.get("TELEGRAM_BOT_TOKEN") or config.get("telegram", {}).get("bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")   or config.get("telegram", {}).get("chat_id", "")
    if not token or not chat_id:
        logger.warning("Telegram not configured — weekly summary not sent")
        return
    try:
        url = f"{TELEGRAM_API}/bot{token}/sendMessage"
        httpx.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
        logger.info("Weekly summary sent to Telegram")
    except Exception as e:
        logger.error(f"Failed to send weekly summary: {e}")


def get_weekly_summary() -> str:
    """Build and return the weekly summary string (for /status inline button)."""
    trades = load_trade_history(days=7)
    closed = [t for t in trades if t.get("outcome") is not None]
    if not closed:
        return "📊 <b>Weekly Summary</b>\n\nNo closed trades in the past 7 days."
    stats = compute_stats(closed)
    return _build_summary_text(stats, changes=[])


def _build_summary_text(stats: dict, changes: list[str]) -> str:
    win_rate = stats.get("win_rate", 0)
    avg_rr   = stats.get("avg_rr", 0)
    pf       = stats.get("profit_factor", 0)
    dd       = stats.get("max_drawdown_streak", 0)
    total    = stats.get("total", 0)
    wins     = stats.get("wins", 0)

    lines = [
        "📊 <b>Weekly Performance Review</b>",
        f"Period: last 7 days | Closed trades: {total}",
        "",
        f"Win Rate:       {win_rate:.0%} ({wins}/{total})",
        f"Avg RR:         {avg_rr:.2f}",
        f"Profit Factor:  {pf:.2f}",
        f"Max DD Streak:  {dd} losses",
        "",
        "<b>By Market:</b>",
    ]
    for market, data in stats.get("by_market", {}).items():
        wr = data.get("win_rate", 0)
        emoji = "🟢" if wr >= 0.5 else "🔴"
        lines.append(
            f"  {emoji} {market}: {wr:.0%} win | {data.get('total', 0)} trades | "
            f"avg RR {data.get('avg_rr', 0):.2f}"
        )

    if changes:
        lines.extend(["", "<b>Config Changes Made:</b>"])
        for c in changes:
            lines.append(f"  • {c}")
    else:
        lines.extend(["", "No config changes this week."])

    lines.append(f"\n<i>Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</i>")
    return "\n".join(lines)


# ── Main entry point ──────────────────────────────────────────────────────────

def run_weekly_review() -> None:
    """Full weekly review: analyze → adjust → save config → send summary."""
    logger.info("=== Weekly Review Started ===")

    trades = load_trade_history(days=7)
    closed = [t for t in trades if t.get("outcome") is not None]

    logger.info(f"Total trades (7 days): {len(trades)} | Closed: {len(closed)}")

    if len(closed) < 10:
        msg = (
            f"📊 <b>Weekly Review</b>\n\n"
            f"Only {len(closed)} closed trades this week — "
            f"need at least 10 for meaningful analysis.\n"
            f"Keep trading and the system will self-adjust automatically."
        )
        logger.info("Insufficient data for analysis — skipping config update")
        _send_telegram_summary(msg)
        return

    stats = compute_stats(closed)

    config = load_config()
    updated_config, changes = adjust_scoring_thresholds(stats, config)

    if changes:
        save_config(updated_config)
        logger.info(f"Config updated: {changes}")
    else:
        logger.info("No config changes required this week")

    summary = _build_summary_text(stats, changes)
    _send_telegram_summary(summary)
    logger.info("=== Weekly Review Complete ===")


if __name__ == "__main__":
    run_weekly_review()
