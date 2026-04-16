"""
Bot 4: Trigger Bot
Detects lower timeframe entry triggers: BOS, displacement candles, volume expansion.
Uses 5m for BOS, 1m for displacement confirmation.
"""

from __future__ import annotations

from utils.config_loader import load_config
from utils.logger import get_logger

logger = get_logger("bot4")

# Minimum setup score to bother with trigger analysis (saves CPU)
MIN_SETUP_SCORE_FOR_TRIGGER = 35


def _detect_ltf_bos(
    highs: list, lows: list, closes: list,
    direction: str, lookback: int = 20
) -> tuple[bool, float | None]:
    """
    Break of Structure on 5m chart.
    Bullish BOS: current close breaks above the highest high of the prior `lookback` bars.
    Bearish BOS: current close breaks below the lowest low of the prior `lookback` bars.
    Returns (bos_confirmed: bool, bos_level: float|None).
    """
    if len(closes) < lookback + 2:
        return False, None

    prior_highs = highs[-(lookback + 1):-1]
    prior_lows  = lows[-(lookback + 1):-1]
    current_close = closes[-1]

    if direction == "LONG":
        pivot = max(prior_highs) if prior_highs else None
        if pivot and current_close > pivot:
            return True, round(pivot, 5)

    elif direction == "SHORT":
        pivot = min(prior_lows) if prior_lows else None
        if pivot and current_close < pivot:
            return True, round(pivot, 5)

    return False, None


def _detect_displacement_candle(
    opens: list, highs: list, lows: list, closes: list,
    direction: str,
    ratio_threshold: float = 0.70,
    lookback: int = 10
) -> tuple[bool, float]:
    """
    A displacement candle has:
    - Body/range ratio >= ratio_threshold (strong directional move)
    - Close in top 20% (bullish) or bottom 20% (bearish) of candle range
    Returns (is_displacement: bool, body_ratio: float).
    """
    if len(closes) < 2:
        return False, 0.0

    last_o = opens[-1]
    last_h = highs[-1]
    last_l = lows[-1]
    last_c = closes[-1]

    candle_range = last_h - last_l
    if candle_range <= 0:
        return False, 0.0

    body = abs(last_c - last_o)
    ratio = body / candle_range

    if ratio < ratio_threshold:
        return False, round(ratio, 4)

    # Close position within candle range
    close_pct = (last_c - last_l) / candle_range

    if direction == "LONG" and close_pct >= 0.80:
        return True, round(ratio, 4)
    if direction == "SHORT" and close_pct <= 0.20:
        return True, round(ratio, 4)

    return False, round(ratio, 4)


def _detect_volume_spike(
    volumes: list, multiplier: float = 1.5, avg_period: int = 20
) -> tuple[bool, float]:
    """
    Detect relative volume expansion.
    Returns (is_spike: bool, volume_ratio: float).
    """
    if len(volumes) < avg_period + 1:
        return False, 0.0

    avg_vol = sum(volumes[-(avg_period + 1):-1]) / avg_period
    if avg_vol <= 0:
        return False, 0.0

    current_vol = volumes[-1]
    ratio = current_vol / avg_vol

    return ratio >= multiplier, round(ratio, 2)


def _detect_displacement_strength(
    highs: list, lows: list, closes: list, direction: str
) -> float:
    """
    Measure how far the last bar moved relative to the previous bar's range.
    Used as a derived metric for displacement strength.
    """
    if len(highs) < 2:
        return 0.0

    prev_range = highs[-2] - lows[-2]
    if prev_range <= 0:
        return 0.0

    if direction == "LONG":
        move = closes[-1] - closes[-2]
    else:
        move = closes[-2] - closes[-1]

    return round(move / prev_range, 2) if move > 0 else 0.0


def _analyze_symbol(sym_data: dict, config: dict) -> dict:
    setup   = sym_data.get("setup", {})
    tf_data = sym_data.get("timeframes", {})
    direction = setup.get("direction", "LONG")

    vol_multiplier    = config.get("volume_spike_multiplier", 1.5)
    disp_ratio_thresh = config.get("displacement_ratio_threshold", 0.70)

    # Use 5m for BOS, 1m for displacement (more granular)
    tf_5  = tf_data.get("5m") or tf_data.get("15m") or {}
    tf_1  = tf_data.get("1m") or tf_5

    h5 = tf_5.get("highs", [])
    l5 = tf_5.get("lows", [])
    c5 = tf_5.get("closes", [])

    h1 = tf_1.get("highs", h5)
    l1 = tf_1.get("lows", l5)
    o1 = tf_1.get("opens", [])
    c1 = tf_1.get("closes", c5)
    v1 = tf_1.get("volumes", tf_5.get("volumes", []))

    # LTF BOS (on 5m)
    bos, bos_level = _detect_ltf_bos(h5, l5, c5, direction)

    # Displacement candle (on 1m)
    o1_fallback = tf_5.get("opens", [])
    disp, disp_ratio = _detect_displacement_candle(
        o1 or o1_fallback, h1, l1, c1, direction, disp_ratio_thresh
    )

    # Volume spike (on 1m volume)
    vol_spike, vol_ratio = _detect_volume_spike(v1, vol_multiplier)

    # Displacement strength metric
    disp_strength = _detect_displacement_strength(h1, l1, c1, direction)

    # Trigger Score (0-100)
    score = 0
    if bos:
        score += 40
    if disp:
        score += 35
    if vol_spike:
        score += 25
    if setup.get("trap_detected"):
        score = max(0, score - 50)  # Heavy penalty for trap

    score = min(100, score)

    return {
        "ltf_bos":             bos,
        "bos_level":           bos_level,
        "bos_direction":       direction if bos else None,
        "displacement_candle": disp,
        "displacement_ratio":  disp_ratio,
        "displacement_strength": disp_strength,
        "volume_spike":        vol_spike,
        "volume_ratio":        vol_ratio,
        "trigger_score":       score,
    }


class TriggerBot:
    """Bot 4: Detects LTF BOS, displacement candles, and volume spikes."""

    def run(self, pipeline_dict: dict) -> dict:
        config  = load_config()
        symbols = pipeline_dict.get("symbols", {})
        processed = skipped = 0

        for sym, sym_data in symbols.items():
            if sym_data.get("fetch_status") != "ok":
                sym_data["trigger"] = {"trigger_score": 0, "skipped": True}
                continue

            setup_score = sym_data.get("setup", {}).get("setup_score", 0)
            if setup_score < MIN_SETUP_SCORE_FOR_TRIGGER:
                sym_data["trigger"] = {
                    "trigger_score": 0,
                    "skipped": True,
                    "skip_reason": f"setup_score {setup_score} < {MIN_SETUP_SCORE_FOR_TRIGGER}",
                }
                skipped += 1
                continue

            try:
                sym_data["trigger"] = _analyze_symbol(sym_data, config)
                processed += 1
                t = sym_data["trigger"]
                logger.info(
                    f"  {sym}: bos={t.get('ltf_bos')} "
                    f"disp={t.get('displacement_candle')} "
                    f"vol={t.get('volume_spike')} "
                    f"score={t.get('trigger_score')}"
                )
            except Exception as e:
                logger.error(f"  {sym} trigger error: {e}")
                sym_data["trigger"] = {"trigger_score": 0, "error": str(e)}

        logger.info(f"Bot 4 complete: {processed} analyzed, {skipped} skipped (weak setup)")
        return pipeline_dict
