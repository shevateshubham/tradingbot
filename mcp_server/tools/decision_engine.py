"""
decision_engine.py — SMC trade decision scoring engine.
Applies confluence weights from config.py and returns a scored trade decision.
"""
from dataclasses import dataclass, field
from typing import Any, List

from mcp_server.config import SCORE_WEIGHTS, SCORE_PENALTIES, GRADE_THRESHOLDS


@dataclass
class TradeDecision:
    send: bool
    score: float
    grade: str           # "A+" | "A" | "B" | "C"
    narrative: str
    evidence: List[str] = field(default_factory=list)


def score_decision(
    weekly_trend: str,
    daily_structure: str,
    h4_flow: str,
    signal_direction: str,
    institutional: Any,
    poi_type: str,
    trap_confirmed: bool,
    ltf_choch: bool,
    volume_spike: bool,
    in_discount: bool,
    is_index: bool,
    is_killzone: bool,
    is_session_open: bool,
    htf_ob_confluence: bool,
    first_touch_ob: bool,
    ob_already_touched: bool,
    segment: str,
    # Options-specific (indices / FNO only)
    pcr_confirms: bool = False,
    near_max_pain: bool = False,
    gex_supports: bool = False,
    options_conflict: bool = False,
    # Session quality
    is_lunch_hour: bool = False,
    low_volume_session: bool = False,
) -> TradeDecision:
    """
    Score a potential SMC trade on 0–100 scale using config.py weights.

    Parameters
    ----------
    weekly_trend, daily_structure, h4_flow : "BULLISH" | "BEARISH" | "NEUTRAL"
    signal_direction : "LONG" | "SHORT"
    institutional    : InstitutionalAnalysis object from institutional_detector
    poi_type         : "OB" | "OB_FVG" | "BREAKER"
    trap_confirmed   : liquidity trap aligned with signal direction
    ltf_choch        : lower-timeframe change of character confirmed
    volume_spike     : volume > 1.5× average
    in_discount      : price below equilibrium (bullish setup zone)
    is_index         : True for index instruments
    is_killzone      : within ±30 min of session open
    is_session_open  : session is currently active
    htf_ob_confluence: higher-timeframe OB aligns with entry
    first_touch_ob   : OB has never been retested before
    ob_already_touched: OB has been mitigated
    segment          : market segment string

    Returns
    -------
    TradeDecision
    """
    score = 0.0
    evidence: List[str] = []

    # ── OB entry ───────────────────────────────────────────────────────
    if poi_type in ("OB", "OB_FVG", "BREAKER"):
        score += SCORE_WEIGHTS["ob_entry"]
        evidence.append(f"OB entry ({poi_type}) +{SCORE_WEIGHTS['ob_entry']}")

    # ── OB + FVG overlap ───────────────────────────────────────────────
    if poi_type == "OB_FVG" or getattr(institutional, "propulsion_block", False):
        score += SCORE_WEIGHTS["ob_fvg_overlap"]
        evidence.append(f"OB+FVG confluence +{SCORE_WEIGHTS['ob_fvg_overlap']}")

    # ── HTF bias alignment ─────────────────────────────────────────────
    # Map signal_direction to bias labels for comparison
    bull_dir = signal_direction == "LONG"
    htf_aligned = any(
        t == ("BULLISH" if bull_dir else "BEARISH")
        for t in (weekly_trend, daily_structure, h4_flow)
        if t != "NEUTRAL"
    )
    htf_conflict = (
        weekly_trend not in ("NEUTRAL", "BULLISH" if bull_dir else "BEARISH")
        and daily_structure not in ("NEUTRAL", "BULLISH" if bull_dir else "BEARISH")
    )

    if htf_aligned:
        score += SCORE_WEIGHTS["htf_bias_match"]
        evidence.append(f"HTF bias aligned +{SCORE_WEIGHTS['htf_bias_match']}")

    if htf_conflict:
        score += SCORE_PENALTIES["htf_conflict"]
        evidence.append(f"HTF conflict {SCORE_PENALTIES['htf_conflict']}")

    # ── Premium / Discount zone ────────────────────────────────────────
    zone_aligned = (signal_direction == "LONG" and in_discount) or \
                   (signal_direction == "SHORT" and not in_discount)
    if zone_aligned:
        score += SCORE_WEIGHTS["premium_discount"]
        zone_label = "Discount" if in_discount else "Premium"
        evidence.append(f"{zone_label} zone +{SCORE_WEIGHTS['premium_discount']}")

    # ── Liquidity swept ────────────────────────────────────────────────
    liq_event = getattr(institutional, "liquidity_event", None)
    liq_val = getattr(liq_event, "value", "NONE")
    if liq_val != "NONE":
        score += SCORE_WEIGHTS["liquidity_swept"]
        evidence.append(f"Liquidity swept ({liq_val}) +{SCORE_WEIGHTS['liquidity_swept']}")

    # ── Killzone ───────────────────────────────────────────────────────
    if is_killzone:
        score += SCORE_WEIGHTS["killzone"]
        evidence.append(f"In killzone +{SCORE_WEIGHTS['killzone']}")

    # ── LTF CHOCH ─────────────────────────────────────────────────────
    if ltf_choch:
        score += SCORE_WEIGHTS["ltf_choch"]
        evidence.append(f"LTF CHOCH +{SCORE_WEIGHTS['ltf_choch']}")

    # ── Session bias ───────────────────────────────────────────────────
    if is_session_open:
        score += SCORE_WEIGHTS["session_bias"]
        evidence.append(f"Active session +{SCORE_WEIGHTS['session_bias']}")

    # ── Volume ratio ───────────────────────────────────────────────────
    if volume_spike:
        score += SCORE_WEIGHTS["volume_ratio"]
        evidence.append(f"Volume spike +{SCORE_WEIGHTS['volume_ratio']}")

    # ── Options / index-specific scoring ──────────────────────────────
    if pcr_confirms:
        score += SCORE_WEIGHTS["options_confirm"]
        evidence.append(f"PCR confirms direction +{SCORE_WEIGHTS['options_confirm']}")

    if near_max_pain:
        score += SCORE_WEIGHTS["max_pain_proximity"]
        evidence.append(f"Near max pain +{SCORE_WEIGHTS['max_pain_proximity']}")

    if gex_supports:
        score += 3
        evidence.append("GEX supports direction +3")

    if options_conflict:
        score -= 5
        evidence.append("Options conflict -5")

    # ── Session quality penalties ──────────────────────────────────────
    if is_lunch_hour:
        score += SCORE_PENALTIES["lunch_hour"]
        evidence.append(f"Lunch hour {SCORE_PENALTIES['lunch_hour']}")

    if low_volume_session:
        score -= 3
        evidence.append("Low volume session -3")

    # ── HTF OB confluence (breaker block counts) ───────────────────────
    if htf_ob_confluence:
        score += 5
        evidence.append("HTF OB confluence +5")

    # ── OB first touch (bonus) ─────────────────────────────────────────
    if first_touch_ob:
        score += 3
        evidence.append("First touch OB +3")

    # ── OB already touched (penalty) ──────────────────────────────────
    if ob_already_touched:
        score += SCORE_PENALTIES["ob_already_touched"]
        evidence.append(f"OB mitigated {SCORE_PENALTIES['ob_already_touched']}")

    # ── Trap confirmation ──────────────────────────────────────────────
    if trap_confirmed:
        evidence.append("Trap confirmed (entry trigger)")

    # ── Institutional bias alignment ───────────────────────────────────
    inst_bias = getattr(institutional, "institutional_bias", "NEUTRAL")
    inst_score = getattr(institutional, "total_score", 0)
    expected_bias = "BULLISH" if signal_direction == "LONG" else "BEARISH"
    if inst_bias == expected_bias:
        bonus = round(min(8.0, inst_score * 0.1), 1)
        score += bonus
        evidence.append(f"Institutional bias: {inst_bias} +{bonus}")
    elif inst_bias not in ("NEUTRAL",) and inst_bias != expected_bias:
        score -= 5
        evidence.append(f"Institutional bias conflict ({inst_bias} vs {expected_bias}) -5")

    # ── Clamp ─────────────────────────────────────────────────────────
    score = max(0.0, min(100.0, score))

    # ── Grade ─────────────────────────────────────────────────────────
    if score >= GRADE_THRESHOLDS["A_PLUS"]:
        grade = "A+"
    elif score >= GRADE_THRESHOLDS["A"]:
        grade = "A"
    elif score >= GRADE_THRESHOLDS["B"]:
        grade = "B"
    else:
        grade = "C"

    # ── Send gate (min confluence score from settings) ─────────────────
    try:
        from mcp_server.config import get_settings
        min_score = get_settings().min_confluence_score
    except Exception:
        min_score = 72
    send = score >= min_score

    # ── Narrative ─────────────────────────────────────────────────────
    zone_label = "Discount" if in_discount else "Premium"
    kz_tag = " | KZ" if is_killzone else ""
    liq_tag = f" | Liq:{liq_val}" if liq_val != "NONE" else ""
    narrative = (
        f"{signal_direction} @ {zone_label} | {poi_type}"
        f"{liq_tag}{kz_tag} | Score:{score:.0f} ({grade})"
    )

    return TradeDecision(
        send=send,
        score=round(score, 1),
        grade=grade,
        narrative=narrative,
        evidence=evidence,
    )
