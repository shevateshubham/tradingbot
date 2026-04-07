"""
tools/trade_scorer.py — Confluence scoring engine.
Applies all scoring weights, penalties, and hard discard rules.
Returns final score, grade, and rejection reason if applicable.
"""

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from mcp_server.config import (
    SCORE_WEIGHTS, SCORE_PENALTIES, GRADE_THRESHOLDS,
    MAX_SPREAD_PERCENT, get_settings,
)

logger = logging.getLogger(__name__)


@dataclass
class ScoreResult:
    """Result of the scoring engine."""
    score:          int
    grade:          str              # A+, A, B, C
    passed:         bool             # True if score >= send threshold
    reject_reason:  Optional[str]    # Set if hard-discarded
    breakdown:      dict             # Per-factor scores for debugging


def _grade_from_score(score: int) -> str:
    if score >= GRADE_THRESHOLDS["A_PLUS"]:
        return "A+"
    elif score >= GRADE_THRESHOLDS["A"]:
        return "A"
    elif score >= GRADE_THRESHOLDS["B"]:
        return "B"
    else:
        return "C"


def score_signal(enriched: dict) -> ScoreResult:
    """
    Calculate confluence score from enriched payload.
    Applies all weights and penalties from config.

    Hard discard rules checked independently of score.
    Returns ScoreResult with final score, grade, and pass/fail.
    """
    breakdown = {}
    score = 0

    direction        = enriched.get("direction", "LONG")
    htf_bias         = enriched.get("htf_bias", "NEUTRAL")
    signal_type      = enriched.get("signal_type", "")
    fvg_present      = enriched.get("fvg_present", False)
    htf_matches      = enriched.get("htf_matches_direction", False)
    in_discount      = enriched.get("in_discount")
    liquidity_swept  = enriched.get("liquidity_swept", False)
    is_killzone      = enriched.get("is_killzone", False)
    ltf_choch        = enriched.get("ltf_choch", False)
    session          = enriched.get("session", "")
    options_confirm  = enriched.get("options_confirm", False)
    near_max_pain    = enriched.get("near_max_pain", False)
    volume_ratio     = enriched.get("volume_ratio", 0.0)
    in_lunch         = enriched.get("in_lunch", False)
    trap_present     = enriched.get("trap_present", False)
    trap_direction   = enriched.get("trap_direction", "")
    segment          = enriched.get("segment", "")
    close            = enriched.get("close", 0.0)

    # ─────────────────────────────────────────────────────────────
    # POSITIVE WEIGHTS
    # ─────────────────────────────────────────────────────────────

    # +20: OB entry signal
    if signal_type in ("OB_ENTRY", "OB"):
        pts = SCORE_WEIGHTS["ob_entry"]
        score += pts
        breakdown["ob_entry"] = pts

    # +18: OB + FVG overlap
    if signal_type in ("OB_ENTRY", "OB") and fvg_present:
        pts = SCORE_WEIGHTS["ob_fvg_overlap"]
        score += pts
        breakdown["ob_fvg_overlap"] = pts

    # +15: HTF bias matches trade direction
    if htf_matches:
        pts = SCORE_WEIGHTS["htf_bias_match"]
        score += pts
        breakdown["htf_bias_match"] = pts

    # +12: Correct premium/discount zone
    correct_zone = (
        (direction == "LONG" and in_discount is True) or
        (direction == "SHORT" and in_discount is False)
    )
    if correct_zone:
        pts = SCORE_WEIGHTS["premium_discount"]
        score += pts
        breakdown["premium_discount"] = pts

    # +10: Liquidity swept before POI
    if liquidity_swept:
        pts = SCORE_WEIGHTS["liquidity_swept"]
        score += pts
        breakdown["liquidity_swept"] = pts

    # +8: Inside killzone window
    if is_killzone:
        pts = SCORE_WEIGHTS["killzone"]
        score += pts
        breakdown["killzone"] = pts

    # +8: LTF CHOCH confirmed
    if ltf_choch:
        pts = SCORE_WEIGHTS["ltf_choch"]
        score += pts
        breakdown["ltf_choch"] = pts

    # +5: Session bias matches (London/NY preferred)
    session_bonus_sessions = {"LONDON", "NY", "INDIA"}
    if session.upper() in session_bonus_sessions:
        pts = SCORE_WEIGHTS["session_bias"]
        score += pts
        breakdown["session_bias"] = pts

    # +5: Options confirms direction (indices/FNO only)
    if segment in ("INDICES", "INDIAN_FNO") and options_confirm:
        pts = SCORE_WEIGHTS["options_confirm"]
        score += pts
        breakdown["options_confirm"] = pts

    # +4: Near max pain on expiry week (indices only)
    if segment == "INDICES" and near_max_pain:
        pts = SCORE_WEIGHTS["max_pain_proximity"]
        score += pts
        breakdown["max_pain_proximity"] = pts

    # +3: Volume ratio > 1.5×
    if volume_ratio >= 1.5:
        pts = SCORE_WEIGHTS["volume_ratio"]
        score += pts
        breakdown["volume_ratio"] = pts

    # ─────────────────────────────────────────────────────────────
    # PENALTIES
    # ─────────────────────────────────────────────────────────────

    # -10: HTF and signal direction conflict
    htf_conflict = (
        (direction == "LONG" and htf_bias == "BEARISH") or
        (direction == "SHORT" and htf_bias == "BULLISH")
    )
    if htf_conflict:
        pts = SCORE_PENALTIES["htf_conflict"]
        score += pts  # Negative
        breakdown["htf_conflict"] = pts

    # -8: OB already touched once (mitigated OB, weaker)
    ob_touched = enriched.get("ob_already_touched", False)
    if ob_touched:
        pts = SCORE_PENALTIES["ob_already_touched"]
        score += pts
        breakdown["ob_already_touched"] = pts

    # -5: Lunch hour (12:00–13:30 IST)
    if in_lunch:
        pts = SCORE_PENALTIES["lunch_hour"]
        score += pts
        breakdown["lunch_hour"] = pts

    # -5: Trap in same direction as signal (Judas/Bull-Bear trap risk)
    trap_same_dir = trap_present and (
        (direction == "LONG" and trap_direction.upper() in ("BULL", "BULLISH")) or
        (direction == "SHORT" and trap_direction.upper() in ("BEAR", "BEARISH"))
    )
    if trap_same_dir:
        pts = SCORE_PENALTIES["trap_same_dir"]
        score += pts
        breakdown["trap_same_dir"] = pts

    # Clamp score to 0–100
    score = max(0, min(100, score))
    grade = _grade_from_score(score)

    # ─────────────────────────────────────────────────────────────
    # HARD DISCARD RULES (independent of score)
    # ─────────────────────────────────────────────────────────────

    settings = get_settings()

    # HTF conflict with Daily structure
    if htf_conflict and htf_bias != "NEUTRAL":
        logger.info(f"Hard discard: HTF conflict — direction={direction} htf_bias={htf_bias}")
        return ScoreResult(
            score=score, grade=grade, passed=False,
            reject_reason=f"against_daily_structure:direction={direction} htf={htf_bias}",
            breakdown=breakdown,
        )

    # Score below minimum send threshold
    min_score = settings.min_confluence_score
    if score < min_score:
        logger.info(f"Hard discard: score {score} < min {min_score}")
        return ScoreResult(
            score=score, grade=grade, passed=False,
            reject_reason=f"score_too_low:{score}<{min_score}",
            breakdown=breakdown,
        )

    # Grade must be A or better to send
    if grade in ("B", "C"):
        logger.info(f"Hard discard: grade={grade}")
        return ScoreResult(
            score=score, grade=grade, passed=False,
            reject_reason=f"grade_below_threshold:{grade}",
            breakdown=breakdown,
        )

    # RR check is done in calculator — if RR < min, calculator rejects

    logger.info(
        f"Score: {score}/100 grade={grade} breakdown={breakdown}"
    )
    return ScoreResult(
        score=score, grade=grade, passed=True,
        reject_reason=None,
        breakdown=breakdown,
    )
