"""
Bot 6: Notification Bot
On-demand signal delivery — sends a signal for a SPECIFIC symbol chosen by the user.
Also handles: trade log persistence, open trade outcome checking.

Design change: NotificationBot.run() no longer auto-sends all signals.
Signals are sent only when the user taps a symbol in the inline keyboard.
Use send_signal_for_symbol() for targeted, chat-specific delivery.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from utils.config_loader import load_config
from utils.logger import get_logger

logger = get_logger("bot6")

TRADES_DIR  = Path(__file__).parent.parent / "data" / "trades"
TRADES_DIR.mkdir(parents=True, exist_ok=True)

TELEGRAM_API = "https://api.telegram.org"


# ── Telegram delivery ──────────────────────────────────────────────────────────

def _send_telegram(
    text: str,
    config: dict,
    target_chat_id: str | None = None,
    parse_mode: str = "HTML",
) -> int | None:
    """
    Send a Telegram message to a specific chat (or the default TELEGRAM_CHAT_ID).
    Returns message_id on success, None on failure. Retries once.
    """
    token   = os.environ.get("TELEGRAM_BOT_TOKEN") or config.get("telegram", {}).get("bot_token", "")
    chat_id = target_chat_id or \
              os.environ.get("TELEGRAM_CHAT_ID") or \
              config.get("telegram", {}).get("chat_id", "")

    if not token or not chat_id:
        logger.error("Telegram credentials not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return None

    url     = f"{TELEGRAM_API}/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}

    for attempt in range(2):
        try:
            resp = httpx.post(url, json=payload, timeout=10.0)
            if resp.status_code == 200:
                return resp.json().get("result", {}).get("message_id")
            logger.warning(f"Telegram API {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Telegram send error (attempt {attempt + 1}): {e}")
            if attempt == 0:
                time.sleep(2)
    return None


# ── Message formatting ─────────────────────────────────────────────────────────

def _fmt(v) -> str:
    """Format a price value nicely regardless of magnitude."""
    if v is None:
        return "—"
    if abs(v) >= 100:
        return f"{v:,.2f}"
    return f"{v:.5f}"


def _format_trade_message(sym: str, sym_data: dict) -> str:
    """Full trade signal message (HTML)."""
    decision  = sym_data.get("decision", {})
    ctx       = sym_data.get("context", {})
    setup     = sym_data.get("setup", {})
    trigger   = sym_data.get("trigger", {})
    market    = sym_data.get("market", "")

    direction  = decision.get("direction", "?")
    confidence = decision.get("confidence_score", 0)
    grade      = decision.get("grade", "?")
    entry      = decision.get("entry")
    sl         = decision.get("sl")
    tp1        = decision.get("tp1")
    tp2        = decision.get("tp2")
    tp3        = decision.get("tp3")
    rr         = decision.get("rr_ratio", 0)
    sl_pct     = decision.get("sl_percent", 0)

    dir_emoji   = "🟢" if direction == "LONG" else "🔴"
    grade_emoji = {"A+": "⭐", "A": "✅", "B": "👀"}.get(grade, "")

    # Confluence tags
    tags = []
    htf = ctx.get("htf_bias", "?")
    if htf != "NEUTRAL":
        tags.append(f"HTF {htf}")
    sweep = setup.get("sweep_type", "NONE")
    if sweep != "NONE":
        tags.append(sweep)
    ob = setup.get("order_block", {})
    if ob.get("found"):
        tags.append("OB" + ("" if not ob.get("already_touched") else " (used)"))
    if setup.get("fvg", {}).get("present"):
        tags.append("FVG")
    if setup.get("inducement_detected"):
        tags.append("IND")
    if trigger.get("ltf_bos"):
        tags.append("BOS")
    if trigger.get("displacement_candle"):
        tags.append("DISP")
    if trigger.get("volume_spike"):
        tags.append(f"VOL {trigger.get('volume_ratio', 0):.1f}x")
    if setup.get("trap_detected"):
        tags.append("⚠️TRAP")

    bd = decision.get("score_breakdown", {})
    cw = bd.get("context_weighted", 0)
    sw = bd.get("setup_weighted", 0)
    tw = bd.get("trigger_weighted", 0)
    ib = bd.get("inducement_bonus", 0)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return "\n".join([
        f"{dir_emoji} <b>SIGNAL — {sym} {direction}</b>",
        f"Market: {market}  |  Grade: {grade_emoji} {grade}  |  Confidence: {confidence:.0f}%",
        "",
        f"Entry  <code>{_fmt(entry)}</code>",
        f"SL     <code>{_fmt(sl)}</code>  ({sl_pct:.2f}%)",
        f"TP1    <code>{_fmt(tp1)}</code>  (1:1 RR)",
        f"TP2    <code>{_fmt(tp2)}</code>  (2:1 RR)",
        f"TP3    <code>{_fmt(tp3)}</code>  (3:1 RR)",
        f"RR     {rr:.1f} : 1",
        "",
        f"Confluences: {' · '.join(tags) if tags else '—'}",
        "",
        f"Score breakdown:",
        f"  Context  {bd.get('context_score', 0):.0f} × 40% = {cw:.1f}",
        f"  Setup    {bd.get('setup_score', 0):.0f} × 40% = {sw:.1f}",
        f"  Trigger  {bd.get('trigger_score', 0):.0f} × 20% = {tw:.1f}",
        (f"  Inducement bonus: +{ib}" if ib else ""),
        f"  Total: {confidence:.1f}%",
        "",
        f"<i>{now}</i>",
    ])


def _format_watch_message(sym: str, sym_data: dict) -> str:
    """Compact watch/monitoring message."""
    decision  = sym_data.get("decision", {})
    ctx       = sym_data.get("context", {})
    setup     = sym_data.get("setup", {})
    market    = sym_data.get("market", "")
    direction = decision.get("direction", "?")
    confidence = decision.get("confidence_score", 0)
    htf        = ctx.get("htf_bias", "?")
    setup_type = setup.get("setup_type", "—")
    reject     = decision.get("reject_reason", "")

    return (
        f"👀 <b>WATCH — {sym} {direction}</b>\n"
        f"Market: {market}  |  Conf: {confidence:.0f}%  |  HTF: {htf}\n"
        f"Setup: {setup_type}\n"
        f"<i>Reason: {reject or 'Below confidence threshold — monitor for trigger'}</i>"
    )


def _format_no_trade_message(sym: str, sym_data: dict) -> str:
    """Brief no-trade message (user tapped a ❌ symbol)."""
    decision   = sym_data.get("decision", {})
    confidence = decision.get("confidence_score", 0)
    grade      = decision.get("grade", "C")
    reject     = decision.get("reject_reason", "Low confluence")
    return (
        f"❌ <b>NO TRADE — {sym}</b>\n"
        f"Confidence: {confidence:.0f}% [{grade}]\n"
        f"Reason: {reject}"
    )


# ── Trade log persistence ──────────────────────────────────────────────────────

def _save_trade(sym: str, sym_data: dict, run_id: str, msg_id: int | None) -> None:
    """Append trade record to data/trades/YYYY-MM-DD.json for weekly review."""
    today    = datetime.utcnow().strftime("%Y-%m-%d")
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
        "outcome":    None,    # Updated later by check_open_trade_outcomes()
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


# ── Open trade outcome checker ─────────────────────────────────────────────────

def check_open_trade_outcomes(pipeline_dict: dict) -> None:
    """
    Compare current prices to open trades and update outcomes.
    Called at the start of each pipeline scoring run.
    """
    today    = datetime.utcnow().strftime("%Y-%m-%d")
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
            continue
        sym      = trade.get("symbol")
        price    = (symbols.get(sym) or {}).get("current_price")
        if not price:
            continue

        direction = trade.get("direction", "LONG")
        sl, tp1, tp2, tp3 = (
            trade.get("sl", 0), trade.get("tp1", 0),
            trade.get("tp2", 0), trade.get("tp3", 0),
        )

        if direction == "LONG":
            if   price >= tp3: trade["outcome"] = "TP3"; trade["actual_rr"] = 3.0;  changed = True
            elif price >= tp2: trade["outcome"] = "TP2"; trade["actual_rr"] = 2.0;  changed = True
            elif price >= tp1: trade["outcome"] = "TP1"; trade["actual_rr"] = 1.0;  changed = True
            elif price <= sl:  trade["outcome"] = "SL";  trade["actual_rr"] = -1.0; changed = True
        else:
            if   price <= tp3: trade["outcome"] = "TP3"; trade["actual_rr"] = 3.0;  changed = True
            elif price <= tp2: trade["outcome"] = "TP2"; trade["actual_rr"] = 2.0;  changed = True
            elif price <= tp1: trade["outcome"] = "TP1"; trade["actual_rr"] = 1.0;  changed = True
            elif price >= sl:  trade["outcome"] = "SL";  trade["actual_rr"] = -1.0; changed = True

    if changed:
        with open(filepath, "w") as f:
            json.dump(trades, f, indent=2)
        logger.info("Updated open trade outcomes")


# ── Public API ─────────────────────────────────────────────────────────────────

def send_signal_for_symbol(
    sym: str,
    sym_data: dict,
    config: dict,
    run_id: str = "unknown",
    target_chat_id: str | None = None,
) -> str:
    """
    Send the full trade signal for a single symbol to a specific Telegram chat.
    Called when the user taps a symbol button in the inline keyboard.

    Returns the formatted message text (also sent to Telegram).
    """
    decision = sym_data.get("decision", {})
    action   = decision.get("action", "NO_TRADE")

    if action == "TRADE":
        msg = _format_trade_message(sym, sym_data)
    elif action == "WATCH":
        msg = _format_watch_message(sym, sym_data)
    else:
        msg = _format_no_trade_message(sym, sym_data)

    m_id = _send_telegram(msg, config, target_chat_id=target_chat_id)

    # Save trade to log only for actionable signals
    if action == "TRADE":
        _save_trade(sym, sym_data, run_id, m_id)
        logger.info(f"TRADE signal sent for {sym} → chat {target_chat_id} (msg_id={m_id})")
    elif action == "WATCH":
        logger.info(f"WATCH alert sent for {sym} → chat {target_chat_id}")
    else:
        logger.info(f"NO_TRADE summary sent for {sym} → chat {target_chat_id}")

    return msg


# ── NotificationBot (outcome checker only — no auto-send) ─────────────────────

class NotificationBot:
    """
    Bot 6 in the pipeline.
    Only checks open trade outcomes — does NOT auto-send signals.
    Signals are sent on-demand via send_signal_for_symbol() when user taps a symbol.
    """

    def run(self, pipeline_dict: dict) -> dict:
        # Check outcomes of previously open trades using current prices
        check_open_trade_outcomes(pipeline_dict)

        # Count actionable signals for summary (informational only)
        trades  = sum(
            1 for sd in pipeline_dict.get("symbols", {}).values()
            if sd.get("decision", {}).get("action") == "TRADE"
        )
        watches = sum(
            1 for sd in pipeline_dict.get("symbols", {}).values()
            if sd.get("decision", {}).get("action") == "WATCH"
        )
        summary = pipeline_dict.get("pipeline_summary", {})
        summary.update({"trades": trades, "watches": watches})
        pipeline_dict["pipeline_summary"] = summary

        logger.info(
            f"Bot 6: outcome check done | {trades} TRADE · {watches} WATCH available"
        )
        return pipeline_dict
