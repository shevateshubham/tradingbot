"""
Bot 3: Setup Bot
Detects SMC setups: order blocks, FVGs, liquidity sweeps, inducement, traps.
Pure price action — no indicators. Ported from institutional_detector.py.
"""

from __future__ import annotations

from utils.config_loader import load_config
from utils.logger import get_logger
from utils.trade_math import atr

logger = get_logger("bot3")


def _detect_order_block(
    highs: list, lows: list, opens: list, closes: list,
    direction: str, lookback: int = 30, atr_val: float = 0.0
) -> dict:
    """
    Detect the most recent valid order block scanning from RECENT to OLD.
    Bullish OB: last bearish candle (close < open) before a bullish impulse.
    Bearish OB: last bullish candle (close > open) before a bearish impulse.
    The impulse must move positively in the next 3 bars.
    """
    n = len(highs)
    if n < 8:
        return {"found": False, "ob_high": None, "ob_low": None,
                "ob_mid": None, "already_touched": False, "ob_index": -1}

    # Scan from most recent backward; leave 3 bars after i for subsequent-move check
    start = n - 4          # i+3 must be a valid index → i ≤ n-4
    stop  = max(2, n - lookback - 1)

    ob_found = False
    ob_high  = ob_low = ob_mid = 0.0
    ob_idx   = -1

    for i in range(start, stop, -1):
        if direction == "LONG":
            if closes[i] < opens[i]:                      # bearish OB candle
                if closes[i + 3] > closes[i + 1]:         # bullish impulse after it
                    ob_high = highs[i]; ob_low = lows[i]
                    ob_mid  = (ob_high + ob_low) / 2
                    ob_idx  = i; ob_found = True; break
        else:  # SHORT
            if closes[i] > opens[i]:                      # bullish OB candle
                if closes[i + 1] > closes[i + 3]:         # bearish impulse after it
                    ob_high = highs[i]; ob_low = lows[i]
                    ob_mid  = (ob_high + ob_low) / 2
                    ob_idx  = i; ob_found = True; break

    # OB is mitigated only when price fully breaches through it:
    # LONG OB: invalidated if a subsequent wick closes BELOW ob_low (full fill)
    # SHORT OB: invalidated if a subsequent wick closes ABOVE ob_high (full fill)
    # Retests of the zone (touching ob_high for LONG) are normal and expected.
    already_touched = False
    if ob_found and ob_idx >= 0:
        subsequent_lows  = lows[ob_idx + 1:]
        subsequent_highs = highs[ob_idx + 1:]
        if direction == "LONG" and subsequent_lows:
            already_touched = min(subsequent_lows) < ob_low    # fully breached
        elif direction == "SHORT" and subsequent_highs:
            already_touched = max(subsequent_highs) > ob_high  # fully breached

    return {
        "found":           ob_found,
        "ob_high":         round(ob_high, 5) if ob_found else None,
        "ob_low":          round(ob_low, 5)  if ob_found else None,
        "ob_mid":          round(ob_mid, 5)  if ob_found else None,
        "already_touched": already_touched,
        "ob_index":        ob_idx,
    }


def _detect_fvg(
    highs: list, lows: list, closes: list, direction: str, lookback: int = 20
) -> dict:
    """
    Detect the most recent Fair Value Gap (imbalance).
    Ported from institutional_detector.py:135-150.

    Bullish FVG: high[i-2] < low[i] — gap between candle i-2 and candle i.
    Bearish FVG: low[i-2]  > high[i].
    """
    n = min(lookback, len(highs) - 2)
    for i in range(len(highs) - 1, len(highs) - n - 1, -1):
        if i < 2:
            break
        if direction == "LONG" and highs[i - 2] < lows[i]:
            return {
                "present":  True,
                "fvg_high": round(lows[i], 5),
                "fvg_low":  round(highs[i - 2], 5),
                "type":     "BULLISH",
            }
        if direction == "SHORT" and lows[i - 2] > highs[i]:
            return {
                "present":  True,
                "fvg_high": round(lows[i - 2], 5),
                "fvg_low":  round(highs[i], 5),
                "type":     "BEARISH",
            }
    return {"present": False, "fvg_high": None, "fvg_low": None, "type": None}


def _detect_liquidity_sweep(
    highs: list, lows: list, closes: list,
    swing_highs: list, swing_lows: list,
    lookback: int = 10
) -> tuple[bool, str]:
    """
    Detect if a recent candle swept a liquidity level then closed back inside.

    Bullish sweep (SSL swept): wick below swing low, close above it → buying opportunity.
    Bearish sweep (BSL swept): wick above swing high, close below it → selling opportunity.
    """
    if not swing_highs and not swing_lows:
        return False, "NONE"

    n = min(lookback, len(highs))
    recent_highs = highs[-n:]
    recent_lows  = lows[-n:]
    recent_closes = closes[-n:]

    # SSL sweep (price dipped below swing low and closed back above)
    for swing_low in swing_lows:
        for i in range(len(recent_lows)):
            if recent_lows[i] < swing_low and recent_closes[i] > swing_low:
                return True, "SSL_SWEPT"

    # BSL sweep (price spiked above swing high and closed back below)
    for swing_high in swing_highs:
        for i in range(len(recent_highs)):
            if recent_highs[i] > swing_high and recent_closes[i] < swing_high:
                return True, "BSL_SWEPT"

    return False, "NONE"


def _detect_inducement(
    highs: list, lows: list, closes: list,
    htf_bias: str, swing_highs: list, swing_lows: list,
    lookback: int = 15
) -> bool:
    """
    Detect engineered liquidity (inducement): a minor push against HTF bias
    forming a small swing before a major level — trapping retail traders.
    """
    if len(highs) < lookback:
        return False

    n = min(lookback, len(highs))
    recent_h = highs[-n:]
    recent_l = lows[-n:]

    # For BULLISH bias, look for a minor lower swing (fake bearish push)
    # before price reverses upward into discount
    if htf_bias == "BULLISH" and swing_lows:
        major_low = min(swing_lows)
        # Minor inducement: recent low dips below a mid-point but not to the major swing low
        recent_min = min(recent_l)
        mid_low = (max(recent_h) + major_low) / 2
        if major_low < recent_min < mid_low:
            return True

    # For BEARISH bias, look for a minor higher swing (fake bullish push)
    elif htf_bias == "BEARISH" and swing_highs:
        major_high = max(swing_highs)
        recent_max = max(recent_h)
        mid_high = (min(recent_l) + major_high) / 2
        if mid_high < recent_max < major_high:
            return True

    return False


def _detect_trap(
    highs: list, lows: list, closes: list, volumes: list,
    swing_highs: list, swing_lows: list, lookback: int = 4
) -> tuple[bool, str | None]:
    """
    Detect false breakouts (traps).
    Strict criteria: price breaks AND closes back inside within 4 bars,
    AND volume on the breakout candle was below 60% of average — clearly anemic.
    Lookback kept short (4 bars) to avoid false positives from old candles.
    """
    n = min(lookback, len(highs))
    if n < 4 or not volumes:
        return False, None

    # Use full history for volume average so a short-window spike doesn't distort it
    avg_vol = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 0
    if avg_vol <= 0:
        return False, None

    recent_h = highs[-n:]
    recent_l = lows[-n:]
    recent_c = closes[-n:]
    recent_v = volumes[-n:]

    # Bull trap: wick broke above swing high, closed back below, anemic volume
    for swing_high in swing_highs:
        for i in range(len(recent_h) - 2, 0, -1):
            if recent_h[i] > swing_high and recent_c[i] < swing_high:
                if recent_v[i] < avg_vol * 0.60:
                    return True, "BULL_TRAP"

    # Bear trap: wick broke below swing low, closed back above, anemic volume
    for swing_low in swing_lows:
        for i in range(len(recent_l) - 2, 0, -1):
            if recent_l[i] < swing_low and recent_c[i] > swing_low:
                if recent_v[i] < avg_vol * 0.60:
                    return True, "BEAR_TRAP"

    return False, None


def _infer_direction(htf_bias: str, sweep_type: str) -> str:
    """Infer trade direction from HTF bias and sweep type."""
    if sweep_type == "SSL_SWEPT":
        return "LONG"   # Swept sell-side → expecting buyers
    if sweep_type == "BSL_SWEPT":
        return "SHORT"  # Swept buy-side → expecting sellers
    # Fall back to HTF bias
    if htf_bias == "BULLISH":
        return "LONG"
    return "SHORT"


def _analyze_symbol(sym_data: dict, config: dict, ltf_tf: str = "15m") -> dict:
    tf_data   = sym_data.get("timeframes", {})
    context   = sym_data.get("context", {})
    htf_bias  = context.get("htf_bias", "NEUTRAL")
    swing_h   = context.get("swing_highs", [])
    swing_l   = context.get("swing_lows", [])
    lookback  = config.get("ob_lookback_bars", 30)

    # Use LTF for OB/sweep detection; fall back in ascending granularity order
    tf_setup = (tf_data.get(ltf_tf) or
                tf_data.get("5m") or tf_data.get("15m") or
                tf_data.get("1h") or tf_data.get("4h") or {})

    # FVG uses one finer timeframe than the setup TF for precision
    finer_tf_order = ["1m", "5m", "15m", "1h", "4h", "1d"]
    ltf_idx = next((i for i, k in enumerate(finer_tf_order) if k == ltf_tf), 2)
    finer_tf = finer_tf_order[max(0, ltf_idx - 1)]
    tf_fvg = tf_data.get(finer_tf) or tf_setup

    if not tf_setup:
        return {"setup_score": 0, "error": f"no_setup_tf_data (tried {ltf_tf})"}

    opens   = tf_setup.get("opens", [])
    highs   = tf_setup.get("highs", [])
    lows    = tf_setup.get("lows", [])
    closes  = tf_setup.get("closes", [])
    volumes = tf_setup.get("volumes", [])

    if len(highs) < 10:
        return {"setup_score": 0, "error": "insufficient_bars"}

    # Liquidity sweep (tells us direction)
    swept, sweep_type = _detect_liquidity_sweep(highs, lows, closes, swing_h, swing_l)
    direction = _infer_direction(htf_bias, sweep_type)

    # ATR for impulse threshold
    atr_val = atr(highs, lows, closes)

    # Order block
    ob = _detect_order_block(highs, lows, opens, closes, direction, lookback, atr_val)

    # FVG (on finer timeframe for precision)
    h5 = tf_fvg.get("highs", highs)
    l5 = tf_fvg.get("lows", lows)
    c5 = tf_fvg.get("closes", closes)
    fvg = _detect_fvg(h5, l5, c5, direction)

    # Inducement
    inducement = _detect_inducement(highs, lows, closes, htf_bias, swing_h, swing_l)

    # Trap detection
    trap_detected, trap_type = _detect_trap(highs, lows, closes, volumes, swing_h, swing_l)

    # Inducement reversal: if induced + swept, boost direction confidence
    inducement_reversal = inducement and swept

    # Setup Score (0-100)
    score = 0
    if swept:
        score += 30
    if ob["found"] and not ob["already_touched"]:
        score += 25
    elif ob["found"] and ob["already_touched"]:
        score -= 15
    if fvg["present"]:
        score += 25
    if inducement:
        score += 15
    if context.get("in_discount") and direction == "LONG":
        score += 10
    if context.get("in_premium") and direction == "SHORT":
        score += 10
    score = max(0, min(100, score))

    setup_type_parts = []
    if ob["found"] and not ob["already_touched"]:
        setup_type_parts.append("OB")
    if fvg["present"]:
        setup_type_parts.append("FVG")
    if swept:
        setup_type_parts.append(sweep_type)
    if inducement:
        setup_type_parts.append("IND")

    return {
        "direction":           direction,
        "liquidity_swept":     swept,
        "sweep_type":          sweep_type,
        "inducement_detected": inducement,
        "inducement_reversal": inducement_reversal,
        "order_block":         ob,
        "fvg":                 fvg,
        "trap_detected":       trap_detected,
        "trap_type":           trap_type,
        "atr":                 round(atr_val, 5),
        "setup_score":         score,
        "setup_type":          "+".join(setup_type_parts) or "NONE",
    }


class SetupBot:
    """Bot 3: Detects SMC setups — OB, FVG, sweeps, inducement, traps."""

    def run(self, pipeline_dict: dict) -> dict:
        config  = load_config()
        symbols = pipeline_dict.get("symbols", {})
        ltf_tf  = pipeline_dict.get("trade_type_config", {}).get("ltf_tf", "15m")
        processed = 0

        for sym, sym_data in symbols.items():
            if sym_data.get("fetch_status") != "ok":
                sym_data["setup"] = {"setup_score": 0, "skipped": True}
                continue
            try:
                sym_data["setup"] = _analyze_symbol(sym_data, config, ltf_tf)
                processed += 1
                s  = sym_data["setup"]
                ob = s.get("order_block", {})
                logger.info(
                    f"  {sym}: dir={s.get('direction')} "
                    f"swept={s.get('liquidity_swept')} "
                    f"ob={'✓' if ob.get('found') else '✗'}"
                    f"{'(used)' if ob.get('already_touched') else ''} "
                    f"fvg={s.get('fvg', {}).get('present', False)} "
                    f"trap={s.get('trap_detected')} "
                    f"type={s.get('setup_type')} "
                    f"score={s.get('setup_score')}"
                )
            except Exception as e:
                logger.error(f"  {sym} setup error: {e}")
                sym_data["setup"] = {"setup_score": 0, "error": str(e)}

        logger.info(f"Bot 3 complete: {processed} symbols analyzed")
        return pipeline_dict
