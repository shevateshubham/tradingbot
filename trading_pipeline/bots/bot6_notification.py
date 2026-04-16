"""
Bot 6: Notification Bot
Sends trade signals via Telegram and saves trade logs for weekly review.
Uses direct Telegram Bot API via httpx (no heavy library dependency).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

from utils.config_loader import load_config
from utils.logger import get_logger

logger = get_logger("bot6")

TRADES_DIR = Path(__file__).parent.parent / "data" / "trades"
TRADES_DIR.mkdir(parents=True, exist_ok=True)

TELEGRAM_API = "https://api.telegram.org"


def _send_telegram(text: str, config: dict, parse_mode: str = "HTML") -> int | None:
    """
    Send a Telegram message. Returns message_id on success, None on failure.
    Retries once after 2 seconds on failure.
    """
    token   = os.environ.get("TELEGRAM_BOT_TOKEN") or config.get("telegram", {}).get("bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")   or config.get("telegram", {}).get("chat_id", "")

    if not token or not chat_id:
        logger.error("Telegram credentials not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return None

    url     = f"{TELEGRAM_API}/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}

    for attempt in range(2):
        try:
            resp = httpx.post(url, json=payload, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("result", {}).get("message_id")
            logger.warning(f"Telegram API {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Telegram send error (attempt {attempt+1}): {e}")
            if attempt == 0:
                import time; time.sleep(2)

    return None


def _format_trade_message(sym: str, sym_data: dict) -> str:
    """Format a rich trade signal message (HTML for Telegram)."""
    decision = sym_data.get("decision", {})
    context  = sym_data.get("context", {})
    setup    = sym_data.get("setup", {})
    trigger  = sym_data.get("trigger", {})
    market   = sym_data.get("market", "")

    direction  = decision.get("direction", "?")
    confidence = decision.get("confidence_score", 0)
    grade      = decision.get("grade", "?")
    entry      = decision.get("entry")
    sl         = decision.get("sl")
    tp1        = decision.get("tp1")
    tp2        = decision.get("tp2")
    tp3        = decision.get("tp3")
    rr         = decision.get("rr_ratio", 0)

    dir_emoji = "🟢" if direction == "LONG" else "🔴"
    grade_emoji = {"A+": "⭐", "A": "✅", "B": "👀", "C": "❌"}.get(grade, "")

    # Confluences
    confluences = []
    htf = context.get("htf_bias", "?")
    if htf != "NEUTRAL":
        confluences.append(f"HTF {htf}")
    sweep = setup.get("sweep_type", "NONE")
    if sweep != "NONE":
        confluences.append(sweep)
    ob = setup.get("order_block", {})
    if ob.get("found"):
        confluences.append("OB" + (" (fresh)" if not ob.get("already_touched") else ""))
    if setup.get("fvg", {}).get("present"):
        confluences.append("FVG")
    if setup.get("inducement_detected"):
        confluences.append("IND")
    if trigger.get("ltf_bos"):
        confluences.append("BOS")
    if trigger.get("displacement_candle"):
        confluences.append("DISP")
    if trigger.get("volume_spike"):
        confluences.append(f"VOL {trigger.get('volume_ratio', 0):.1f}x")

    conf_str = " | ".join(confluences) if confluences else "—"

    def fmt(v):
        if v is None:
            return "—"
        if abs(v) >= 100:
            return f"{v:,.2f}"
        return f"{v:.5f}"

    bd = decision.get("score_breakdown", {})

    lines = [
        f"{dir_emoji} <b>TRADE SIGNAL — {sym} {direction}</b>",
        f"Market: {market} | Grade: {grade_emoji} {grade} | Confidence: {confidence:.0f}%",
        "",
        f"Entry:  <code>{fmt(entry)}</code>",
        f"SL:     <code>{fmt(sl)}</code>    ({decision.get('sl_percent', 0):.2f}%)",
        f"TP1:    <code>{fmt(tp1)}</code>   (1:1)",
        f"TP2:    <code>{fmt(tp2)}</code>   (2:1)",
        f"TP3:    <code>{fmt(tp3)}</code>   (3:1)",
        f"RR:     {rr:.1f}:1",
        "",
        f"Confluences: {conf_str}",
        "",
        f"Score breakdown:",
        f"  Context: {bd.get('context_score', 0):.0f} × {bd.get('context_weight', 0.4) if isinstance(bd.get('context_weight'), float) else 0.4:.0%} = {bd.get('context_weighted', 0):.1f}",
        f"  Setup:   {bd.get('setup_score', 0):.0f} × 40% = {bd.get('setup_weighted', 0):.1f}",
        f"  Trigger: {bd.get('trigger_score', 0):.0f} × 20% = {bd.get('trigger_weighted', 0):.1f}",
        "",
        f"<i>Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</i>",
    ]
    return "\n".join(lines)


def _format_watch_message(sym: str, sym_data: dict) -> str:
    """Compact watch alert."""
    decision = sym_data.get("decision", {})
    context  = sym_data.get("context", {})
    setup    = sym_data.get("setup", {})
    market   = sym_data.get("market", "")
    direction = decision.get("direction", "?")
    confidence = decision.get("confidence_score", 0)
    htf = context.get("htf_bias", "?")
    setup_type = setup.get("setup_type", "—")
    return (
        f"👀 <b>WATCH — {sym} {direction}</b>\n"
        f"Market: {market} | Conf: {confidence:.0f}% | HTF: {htf}\n"
        f"Setup: {setup_type} | Not yet triggered — monitor for BOS"
    )


def _save_trade(sym: str, sym_data: dict, run_id: str, msg_id: int | None) -> None:
    """Save trade record to data/trades/YYYY-MM-DD.json for weekly review."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    filepath = TRADES_DIR / f"{today}.json"

    decision = sym_data.get("decision", {})
    record = {
        "run_id":     run_id,
        "symbol":     sym,
        "market":     sym_data.get("market", ""),
        "direction":  decision.get("direction"),
        "confidence": decision.get("confidence_score"),
        "grade":      decision.get("grade"),
        "entry":      decision.get("entry"),
        "sl":         decision.get("sl"),
        "tp1":        decision.get("tp1"),
        "tp2":        decision.get("tp2"),
        "tp3":        decision.get("tp3"),
        "rr_ratio":   decision.get("rr_ratio"),
        "sl_percent": decision.get("sl_percent"),
        "setup_type": sym_data.get("setup", {}).get("setup_type"),
        "htf_bias":   sym_data.get("context", {}).get("htf_bias"),
        "outcome":    None,   # Updated later by outcome checker
        "actual_rr":  None,
        "msg_id":     msg_id,
        "timestamp":  datetime.utcnow().isoformat(),
    }

    trades = []
    if filepath.exists():
        try:
            with open(filepath) as f:
                trades = json.load(f)
        except Exception:
            pass

    trades.append(record)
    with open(filepath, "w") as f:
        json.dump(trades, f, indent=2)


def check_open_trade_outcomes(pipeline_dict: dict) -> None:
    """
    Compare current prices against open trades and update outcomes.
    Called at the start of each pipeline run.
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")
    filepath = TRADES_DIR / f"{today}.json"
    if not filepath.exists():
        return

    try:
        with open(filepath) as f:
            trades = json.load(f)
    except Exception:
        return

    symbols = pipeline_dict.get("symbols", {})
    changed = False

    for trade in trades:
        if trade.get("outcome") is not None:
            continue  # Already closed
        sym = trade.get("symbol")
        sym_data = symbols.get(sym, {})
        price = sym_data.get("current_price")
        if not price:
            continue

        entry = trade.get("entry", 0)
        sl    = trade.get("sl", 0)
        tp1   = trade.get("tp1", 0)
        tp2   = trade.get("tp2", 0)
        tp3   = trade.get("tp3", 0)
        direction = trade.get("direction", "LONG")

        if direction == "LONG":
            if price >= tp3:
                trade["outcome"] = "TP3"; trade["actual_rr"] = 3.0; changed = True
            elif price >= tp2:
                trade["outcome"] = "TP2"; trade["actual_rr"] = 2.0; changed = True
            elif price >= tp1:
                trade["outcome"] = "TP1"; trade["actual_rr"] = 1.0; changed = True
            elif price <= sl:
                trade["outcome"] = "SL";  trade["actual_rr"] = -1.0; changed = True
        else:
            if price <= tp3:
                trade["outcome"] = "TP3"; trade["actual_rr"] = 3.0; changed = True
            elif price <= tp2:
                trade["outcome"] = "TP2"; trade["actual_rr"] = 2.0; changed = True
            elif price <= tp1:
                trade["outcome"] = "TP1"; trade["actual_rr"] = 1.0; changed = True
            elif price >= sl:
                trade["outcome"] = "SL";  trade["actual_rr"] = -1.0; changed = True

    if changed:
        with open(filepath, "w") as f:
            json.dump(trades, f, indent=2)
        logger.info("Updated trade outcomes")


class NotificationBot:
    """Bot 6: Sends Telegram alerts and saves trade logs."""

    def run(self, pipeline_dict: dict) -> dict:
        config  = load_config()
        symbols = pipeline_dict.get("symbols", {})
        run_id  = pipeline_dict.get("run_id", "unknown")
        min_conf = config.get("scoring", {}).get("min_confidence", 70)

        sent_trades = 0
        sent_watches = 0

        # Check outcomes of previously open trades
        check_open_trade_outcomes(pipeline_dict)

        for sym, sym_data in symbols.items():
            decision = sym_data.get("decision", {})
            action   = decision.get("action", "NO_TRADE")
            conf     = decision.get("confidence_score", 0)

            if action == "TRADE" and conf >= min_conf:
                msg  = _format_trade_message(sym, sym_data)
                m_id = _send_telegram(msg, config)
                _save_trade(sym, sym_data, run_id, m_id)
                sym_data["notification"] = {
                    "sent": True,
                    "type": "TRADE",
                    "telegram_message_id": m_id,
                    "sent_at": datetime.utcnow().isoformat(),
                }
                sent_trades += 1
                logger.info(f"  {sym}: TRADE alert sent (msg_id={m_id})")

            elif action == "WATCH":
                msg  = _format_watch_message(sym, sym_data)
                m_id = _send_telegram(msg, config)
                sym_data["notification"] = {
                    "sent": True,
                    "type": "WATCH",
                    "telegram_message_id": m_id,
                    "sent_at": datetime.utcnow().isoformat(),
                }
                sent_watches += 1
                logger.info(f"  {sym}: WATCH alert sent")

            else:
                sym_data["notification"] = {"sent": False, "type": action}

        summary = pipeline_dict.get("pipeline_summary", {})
        summary.update({
            "notifications_sent_trade": sent_trades,
            "notifications_sent_watch": sent_watches,
        })
        pipeline_dict["pipeline_summary"] = summary

        logger.info(f"Bot 6 complete: {sent_trades} TRADE + {sent_watches} WATCH alerts sent")
        return pipeline_dict
