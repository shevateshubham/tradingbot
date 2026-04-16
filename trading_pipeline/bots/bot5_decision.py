"""
Bot 5: Decision Bot
Weighted scoring (Context=40%, Setup=40%, Trigger=20%).
Calculates entry, SL, TP levels. Classifies TRADE / WATCH / NO_TRADE.
MUST NOT require all conditions to be true — weighted scoring handles partial signals.
"""

from __future__ import annotations

from utils.config_loader import load_config
from utils.logger import get_logger
from utils.trade_math import calculate_levels, validate_trade

logger = get_logger("bot5")


def _compute_confidence(
    context_score: float,
    setup_score: float,
    trigger_score: float,
    weights: dict,
) -> float:
    """
    Weighted confidence score (0-100).
    Default: Context=40%, Setup=40%, Trigger=20%.
    """
    score = (
        context_score * weights.get("context_weight", 0.40) +
        setup_score   * weights.get("setup_weight",   0.40) +
        trigger_score * weights.get("trigger_weight", 0.20)
    )
    return round(min(100.0, max(0.0, score)), 2)


def _grade(confidence: float) -> str:
    if confidence >= 85:
        return "A+"
    if confidence >= 70:
        return "A"
    if confidence >= 55:
        return "B"
    return "C"


def _classify_action(
    confidence: float,
    min_confidence: float,
    trap_detected: bool,
) -> tuple[str, str | None]:
    """
    Classify action and determine reject reason.
    Returns (action: str, reject_reason: str | None).
    """
    if trap_detected and confidence < 80:
        return "NO_TRADE", "trap_detected_low_confidence"
    if confidence >= min_confidence:
        return "TRADE", None
    if confidence >= min_confidence * 0.80:
        return "WATCH", f"confidence_{confidence:.0f}_below_{min_confidence}"
    return "NO_TRADE", f"confidence_{confidence:.0f}_below_watch_threshold"


def _decide_symbol(sym: str, sym_data: dict, config: dict) -> dict:
    context = sym_data.get("context", {})
    setup   = sym_data.get("setup", {})
    trigger = sym_data.get("trigger", {})

    context_score = context.get("context_score", 0)
    setup_score   = setup.get("setup_score", 0)
    trigger_score = trigger.get("trigger_score", 0)

    weights      = config.get("scoring", {})
    min_conf     = weights.get("min_confidence",  70)
    max_sl_pct   = weights.get("max_sl_percent",  2.0)
    # Use trade_type min_rr if injected, else scoring default
    tt_min_rr    = sym_data.get("_trade_type_min_rr")
    min_rr       = tt_min_rr if tt_min_rr is not None else weights.get("min_rr_ratio", 2.0)

    # Inducement reversal bonus: engineered liquidity + aligned sweep = +5
    inducement_bonus = 0
    if setup.get("inducement_reversal"):
        inducement_bonus = 5
        logger.info(f"  {sym}: inducement reversal bonus +5")

    confidence = _compute_confidence(context_score, setup_score, trigger_score, weights)
    confidence = min(100.0, confidence + inducement_bonus)
    grade      = _grade(confidence)

    trap_detected = setup.get("trap_detected", False)
    action, reject_reason = _classify_action(confidence, min_conf, trap_detected)

    score_breakdown = {
        "context_score":    context_score,
        "context_weighted": round(context_score * weights.get("context_weight", 0.40), 2),
        "setup_score":      setup_score,
        "setup_weighted":   round(setup_score * weights.get("setup_weight", 0.40), 2),
        "trigger_score":    trigger_score,
        "trigger_weighted": round(trigger_score * weights.get("trigger_weight", 0.20), 2),
        "inducement_bonus": inducement_bonus,
    }

    # Only calculate levels for actionable signals
    if action in ("TRADE", "WATCH"):
        ob      = setup.get("order_block", {})
        fvg     = setup.get("fvg", {})
        direction = setup.get("direction", "LONG")

        ob_high = ob.get("ob_high")
        ob_low  = ob.get("ob_low")

        if ob_high and ob_low:
            fvg_high = fvg.get("fvg_high") if fvg.get("present") else None
            fvg_low  = fvg.get("fvg_low")  if fvg.get("present") else None

            levels = calculate_levels(ob_high, ob_low, direction, fvg_high, fvg_low)
            valid, discard_reason = validate_trade(levels, max_sl_pct, min_rr)

            if not valid:
                action        = "NO_TRADE"
                reject_reason = discard_reason
                levels        = {}
        else:
            # No OB found — cannot compute levels
            if action == "TRADE":
                action        = "WATCH"
                reject_reason = "no_order_block_for_levels"
            levels = {}
    else:
        levels    = {}
        direction = setup.get("direction", "LONG")

    return {
        "action":          action,
        "direction":       setup.get("direction", "LONG"),
        "confidence_score": confidence,
        "grade":           grade,
        "reject_reason":   reject_reason,
        "score_breakdown": score_breakdown,
        **levels,
    }


class DecisionBot:
    """Bot 5: Weighted scoring → TRADE / WATCH / NO_TRADE with levels."""

    def run(self, pipeline_dict: dict) -> dict:
        config  = load_config()
        symbols = pipeline_dict.get("symbols", {})
        min_rr  = pipeline_dict.get("trade_type_config", {}).get("min_rr")
        trades = watches = no_trades = 0

        for sym, sym_data in symbols.items():
            if min_rr is not None:
                sym_data["_trade_type_min_rr"] = min_rr
            if sym_data.get("fetch_status") != "ok":
                sym_data["decision"] = {"action": "NO_TRADE", "skipped": True}
                continue
            try:
                decision = _decide_symbol(sym, sym_data, config)
                sym_data["decision"] = decision
                action = decision["action"]
                conf   = decision["confidence_score"]
                grade  = decision["grade"]

                if action == "TRADE":
                    trades += 1
                    logger.info(
                        f"  {sym}: TRADE [{grade}] conf={conf} "
                        f"dir={decision.get('direction')} "
                        f"entry={decision.get('entry')} sl={decision.get('sl')}"
                    )
                elif action == "WATCH":
                    watches += 1
                    logger.info(f"  {sym}: WATCH [{grade}] conf={conf}")
                else:
                    no_trades += 1
                    logger.debug(
                        f"  {sym}: NO_TRADE conf={conf} reason={decision.get('reject_reason')}"
                    )
            except Exception as e:
                logger.error(f"  {sym} decision error: {e}")
                sym_data["decision"] = {
                    "action": "NO_TRADE", "error": str(e), "confidence_score": 0,
                }

        pipeline_dict["pipeline_summary"] = {
            "trades":    trades,
            "watches":   watches,
            "no_trades": no_trades,
        }
        logger.info(
            f"Bot 5 complete: {trades} TRADE, {watches} WATCH, {no_trades} NO_TRADE"
        )
        return pipeline_dict
