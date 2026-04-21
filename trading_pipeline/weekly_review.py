"""
weekly_review.py — Weekly Signal Analysis & Self-Improvement

Runs every Sunday 8 PM IST via APScheduler.
Analyzes ALL signals sent during the week:
  - TRADE signals: TP1/TP2/TP3 hit vs SL hit vs still open
  - WATCH alerts: how many upgraded to TRADE
  - Quality breakdown by setup type and market
  - Suggested config changes — sent for USER APPROVAL before applying

Approval flow:
  1. Review sent to Telegram with [✅ Apply] [❌ Skip] buttons
  2. User taps Apply → config updated immediately
  3. User taps Skip → config unchanged until next review
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

TRADES_DIR   = Path(__file__).parent / "data" / "trades"
PENDING_FILE = Path(__file__).parent / "data" / "pending_config_changes.json"
TELEGRAM_API = "https://api.telegram.org"


# ── Data Loading ───────────────────────────────────────────────────────────────

def load_week_signals(days: int = 7) -> list[dict]:
    """Load ALL signals (trade records) from the past N days."""
    signals = []
    cutoff  = datetime.now(timezone.utc) - timedelta(days=days)

    for filepath in sorted(TRADES_DIR.glob("*.json")):
        try:
            date_str  = filepath.stem
            file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if file_date < cutoff:
                continue
            with open(filepath) as f:
                day_signals = json.load(f)
            signals.extend(day_signals)
        except Exception as e:
            logger.warning(f"Could not load {filepath}: {e}")

    return signals


# ── Statistics ─────────────────────────────────────────────────────────────────

def compute_stats(signals: list[dict]) -> dict:
    """
    Full stats on all signals — includes open (outcome=None) trades.
    """
    trades = [s for s in signals if s.get("grade") in ("A", "A+")]
    watches = [s for s in signals if s.get("grade") == "B"]

    closed  = [t for t in trades if t.get("outcome") is not None]
    wins    = [t for t in closed if t.get("outcome") in ("TP1", "TP2", "TP3")]
    losses  = [t for t in closed if t.get("outcome") == "SL"]
    open_t  = [t for t in trades if t.get("outcome") is None]

    tp1_hits = sum(1 for t in closed if t.get("outcome") == "TP1")
    tp2_hits = sum(1 for t in closed if t.get("outcome") == "TP2")
    tp3_hits = sum(1 for t in closed if t.get("outcome") == "TP3")

    win_rate = len(wins) / len(closed) if closed else 0.0

    rr_values = [t.get("actual_rr", 0) for t in closed if t.get("actual_rr") is not None]
    avg_rr    = sum(rr_values) / len(rr_values) if rr_values else 0.0

    gross_profit = sum(r for r in rr_values if r > 0)
    gross_loss   = abs(sum(r for r in rr_values if r < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0
    )

    # Max consecutive losses
    streak = max_streak = 0
    for t in closed:
        if t.get("outcome") == "SL":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # Per-setup-type analysis
    by_setup: dict[str, dict] = {}
    for t in closed:
        stype = t.get("setup_type") or "UNKNOWN"
        if stype not in by_setup:
            by_setup[stype] = {"total": 0, "wins": 0, "losses": 0}
        by_setup[stype]["total"] += 1
        if t.get("outcome") in ("TP1", "TP2", "TP3"):
            by_setup[stype]["wins"] += 1
        elif t.get("outcome") == "SL":
            by_setup[stype]["losses"] += 1

    # Per-market analysis
    by_market: dict[str, dict] = {}
    for t in closed:
        mkt = t.get("market", "UNKNOWN")
        if mkt not in by_market:
            by_market[mkt] = {"total": 0, "wins": 0, "losses": 0, "rr_values": []}
        by_market[mkt]["total"] += 1
        if t.get("outcome") in ("TP1", "TP2", "TP3"):
            by_market[mkt]["wins"] += 1
        elif t.get("outcome") == "SL":
            by_market[mkt]["losses"] += 1
        if t.get("actual_rr") is not None:
            by_market[mkt]["rr_values"].append(t["actual_rr"])

    market_stats = {}
    for mkt, d in by_market.items():
        n = d["total"]
        market_stats[mkt] = {
            "total":    n,
            "wins":     d["wins"],
            "losses":   d["losses"],
            "win_rate": d["wins"] / n if n > 0 else 0.0,
            "avg_rr":   sum(d["rr_values"]) / len(d["rr_values"]) if d["rr_values"] else 0.0,
        }

    return {
        "total_signals":   len(signals),
        "total_trades":    len(trades),
        "total_watches":   len(watches),
        "closed":          len(closed),
        "wins":            len(wins),
        "losses":          len(losses),
        "open":            len(open_t),
        "tp1_hits":        tp1_hits,
        "tp2_hits":        tp2_hits,
        "tp3_hits":        tp3_hits,
        "win_rate":        round(win_rate, 4),
        "avg_rr":          round(avg_rr, 4),
        "profit_factor":   round(profit_factor, 4),
        "max_loss_streak": max_streak,
        "by_setup":        by_setup,
        "by_market":       market_stats,
    }


# ── Adaptive Suggestions ───────────────────────────────────────────────────────

def build_suggestions(stats: dict, config: dict) -> tuple[list[str], dict]:
    """
    Build human-readable suggested changes + config_updates dict.
    Does NOT apply changes — returns them for user approval.
    """
    suggestions   = []
    config_updates: dict = {}
    scoring = config.get("scoring", {})
    current_min = scoring.get("min_confidence", 75)
    win_rate    = stats.get("win_rate", 0)
    closed      = stats.get("closed", 0)

    if closed < 5:
        suggestions.append(
            f"Only {closed} closed trades — not enough data to adjust thresholds yet. "
            f"Keep trading for 2-3 more weeks."
        )
        return suggestions, config_updates

    # Confidence threshold adjustment
    if win_rate < 0.35:
        new_min = min(85, current_min + 5)
        if new_min != current_min:
            config_updates["min_confidence"] = new_min
            config_updates["min_confidence_auto"] = new_min
            suggestions.append(
                f"Raise min_confidence {current_min} → {new_min}  "
                f"(win rate {win_rate:.0%} is too low)"
            )
    elif win_rate > 0.65 and closed >= 10:
        new_min = max(60, current_min - 3)
        if new_min != current_min:
            config_updates["min_confidence"] = new_min
            config_updates["min_confidence_auto"] = new_min
            suggestions.append(
                f"Lower min_confidence {current_min} → {new_min}  "
                f"(win rate {win_rate:.0%} is strong — can accept more signals)"
            )

    # Underperforming setup types
    for stype, data in stats.get("by_setup", {}).items():
        if data["total"] >= 5 and (data["wins"] / data["total"]) < 0.25:
            suggestions.append(
                f"Setup '{stype}' has only {data['wins']}/{data['total']} wins — "
                f"consider reducing weight on this pattern"
            )

    # Underperforming markets
    disabled = set(config.get("disabled_symbols", []))
    new_disabled = list(disabled)
    for mkt, data in stats.get("by_market", {}).items():
        if data["total"] >= 5 and data["win_rate"] < 0.20:
            suggestions.append(
                f"Market {mkt}: only {data['win_rate']:.0%} win rate — "
                f"poor market conditions, consider pausing it"
            )

    if not suggestions:
        suggestions.append("Performance looks good — no changes needed this week.")

    if config_updates:
        config_updates["disabled_symbols"] = new_disabled

    return suggestions, config_updates


# ── Pending Changes Store ─────────────────────────────────────────────────────

def save_pending_changes(suggestions: list[str], config_updates: dict) -> None:
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps({
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "suggestions":    suggestions,
        "config_updates": config_updates,
    }, indent=2))


def get_pending_changes() -> dict | None:
    if not PENDING_FILE.exists():
        return None
    try:
        return json.loads(PENDING_FILE.read_text())
    except Exception:
        return None


def apply_pending_changes() -> str:
    """Apply stored pending changes to config.json. Returns summary string."""
    pending = get_pending_changes()
    if not pending:
        return "No pending changes found."

    updates = pending.get("config_updates", {})
    if not updates:
        PENDING_FILE.unlink(missing_ok=True)
        return "No config changes were suggested."

    config = load_config()

    if "min_confidence" in updates:
        config["scoring"]["min_confidence"] = updates["min_confidence"]
    if "min_confidence_auto" in updates:
        config.setdefault("auto_scan", {})["min_confidence_auto"] = updates["min_confidence_auto"]
    if "disabled_symbols" in updates:
        config["disabled_symbols"] = updates["disabled_symbols"]

    save_config(config)
    PENDING_FILE.unlink(missing_ok=True)

    applied = "\n".join(f"• {s}" for s in pending.get("suggestions", []))
    logger.info(f"Pending config changes applied:\n{applied}")
    return applied


def discard_pending_changes() -> None:
    PENDING_FILE.unlink(missing_ok=True)


# ── Message Formatting ─────────────────────────────────────────────────────────

def _build_review_message(stats: dict, suggestions: list[str], has_updates: bool) -> str:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(IST)

    # Date range
    week_start = (now_ist - timedelta(days=7)).strftime("%d %b")
    week_end   = now_ist.strftime("%d %b %Y")

    lines = [
        "📊 <b>Weekly Signal Review</b>",
        f"<i>{week_start} – {week_end}</i>",
        "",
        f"Signals sent: <b>{stats['total_trades']}</b> TRADE  |  <b>{stats['total_watches']}</b> WATCH",
    ]

    closed = stats["closed"]
    if closed > 0:
        wins   = stats["wins"]
        losses = stats["losses"]
        open_n = stats["open"]
        wr     = stats["win_rate"]
        lines += [
            "",
            "<b>TRADE outcomes:</b>",
            f"  ✅ TP hit:     {wins}  (TP1:{stats['tp1_hits']} TP2:{stats['tp2_hits']} TP3:{stats['tp3_hits']})",
            f"  ❌ SL hit:     {losses}",
            f"  ⏳ Still open: {open_n}",
            f"  Win rate: <b>{wr:.0%}</b>  |  Avg RR: <b>{stats['avg_rr']:.2f}x</b>  |  PF: <b>{stats['profit_factor']:.2f}</b>",
        ]
        if stats["max_loss_streak"] >= 3:
            lines.append(f"  ⚠️ Max loss streak: {stats['max_loss_streak']}")
    else:
        lines += ["", "<i>No closed trades yet — outcomes update each scan.</i>"]

    # Setup type breakdown
    by_setup = {k: v for k, v in stats.get("by_setup", {}).items() if v["total"] >= 2}
    if by_setup:
        lines += ["", "<b>Setup performance:</b>"]
        for stype, d in sorted(by_setup.items(), key=lambda x: -x[1]["wins"]):
            wr_s = d["wins"] / d["total"] if d["total"] else 0
            emoji = "🟢" if wr_s >= 0.5 else "🔴"
            lines.append(f"  {emoji} {stype}: {d['wins']}/{d['total']} ({wr_s:.0%})")

    # Market breakdown
    if stats.get("by_market"):
        lines += ["", "<b>By market:</b>"]
        for mkt, d in stats["by_market"].items():
            emoji = "🟢" if d["win_rate"] >= 0.5 else "🔴"
            lines.append(
                f"  {emoji} {mkt}: {d['win_rate']:.0%} WR  "
                f"| {d['total']} trades | avg RR {d['avg_rr']:.2f}x"
            )

    # Suggestions
    lines += ["", "<b>Analysis &amp; suggestions:</b>"]
    for s in suggestions:
        lines.append(f"  • {s}")

    if has_updates:
        lines += ["", "👇 <b>Tap to apply config changes:</b>"]
    else:
        lines += ["", "<i>No config changes required this week.</i>"]

    return "\n".join(lines)


def get_weekly_summary() -> str:
    """For /status inline button — quick read-only summary."""
    signals = load_week_signals(days=7)
    if not signals:
        return "📊 <b>Weekly Summary</b>\n\nNo signals sent in the past 7 days."
    stats = compute_stats(signals)
    config = load_config()
    suggestions, has_updates = build_suggestions(stats, config)
    return _build_review_message(stats, suggestions, bool(has_updates))


# ── Telegram Delivery ──────────────────────────────────────────────────────────

def _send_review_telegram(text: str, reply_markup: dict | None = None) -> None:
    import httpx
    config  = load_config()
    token   = os.environ.get("TELEGRAM_BOT_TOKEN") or config.get("telegram", {}).get("bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")   or config.get("telegram", {}).get("chat_id", "")
    if not token or not chat_id:
        logger.warning("Telegram not configured — weekly review not sent")
        return
    try:
        payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        httpx.post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json=payload, timeout=10,
        )
        logger.info("Weekly review sent to Telegram")
    except Exception as e:
        logger.error(f"Failed to send weekly review: {e}")


# ── Main Entry Point ───────────────────────────────────────────────────────────

def run_weekly_review() -> None:
    logger.info("=== Weekly Review Started ===")

    signals = load_week_signals(days=7)
    logger.info(f"Signals loaded: {len(signals)}")

    stats      = compute_stats(signals)
    config     = load_config()
    suggestions, config_updates = build_suggestions(stats, config)

    has_updates = bool(config_updates)
    message     = _build_review_message(stats, suggestions, has_updates)

    if has_updates:
        save_pending_changes(suggestions, config_updates)
        reply_markup = {
            "inline_keyboard": [[
                {"text": "✅ Apply Changes", "callback_data": "weekly_approve"},
                {"text": "❌ Skip This Week", "callback_data": "weekly_skip"},
            ]]
        }
    else:
        discard_pending_changes()
        reply_markup = None

    _send_review_telegram(message, reply_markup)
    logger.info("=== Weekly Review Complete ===")


if __name__ == "__main__":
    run_weekly_review()
