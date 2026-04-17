"""
Bot 2: Context Bot
Determines higher timeframe bias, liquidity zones, and premium/discount zones.
Pure price action — no indicators. Ported from institutional_detector.py.
"""

from __future__ import annotations

from utils.config_loader import load_config
from utils.logger import get_logger

logger = get_logger("bot2")


def _find_swing_points(highs: list, lows: list, n_side: int = 3) -> tuple[list, list]:
    """
    Fractal-based swing high/low detection.
    Swing high: highs[i] > all highs in [i-n:i+n] (excluding i).
    Returns (swing_highs, swing_lows) as lists of price levels.
    """
    swing_highs = []
    swing_lows  = []
    length = len(highs)

    for i in range(n_side, length - n_side):
        left_h  = highs[max(0, i - n_side): i]
        right_h = highs[i + 1: i + n_side + 1]
        left_l  = lows[max(0, i - n_side): i]
        right_l = lows[i + 1: i + n_side + 1]

        if highs[i] >= max(left_h + right_h, default=highs[i]):
            swing_highs.append(highs[i])
        if lows[i] <= min(left_l + right_l, default=lows[i]):
            swing_lows.append(lows[i])

    # Return only the 5 most recent
    return swing_highs[-5:], swing_lows[-5:]


def _detect_htf_bias(
    highs: list, lows: list, closes: list, lookback: int = 50
) -> tuple[str, int]:
    """
    Determine higher timeframe market bias from 15m data.
    Ported from institutional_detector.py detect_htf_structure logic.

    Returns (bias: str, strength: int 0-3).
    BULLISH  = higher highs + higher lows (HH/HL)
    BEARISH  = lower highs + lower lows (LH/LL)
    NEUTRAL  = mixed or no clear structure
    """
    n = min(lookback, len(highs))
    if n < 10:
        return "NEUTRAL", 0

    h = highs[-n:]
    l = lows[-n:]
    c = closes[-n:]

    # Split into thirds: early, mid, late
    third = n // 3
    h1, h2, h3 = h[:third], h[third:2*third], h[2*third:]
    l1, l2, l3 = l[:third], l[third:2*third], l[2*third:]

    hh_score = 0
    hl_score = 0
    lh_score = 0
    ll_score = 0

    if max(h2) > max(h1):
        hh_score += 1
    if max(h3) > max(h2):
        hh_score += 1
    if min(l2) > min(l1):
        hl_score += 1
    if min(l3) > min(l2):
        hl_score += 1
    if max(h2) < max(h1):
        lh_score += 1
    if max(h3) < max(h2):
        lh_score += 1
    if min(l2) < min(l1):
        ll_score += 1
    if min(l3) < min(l2):
        ll_score += 1

    bullish = hh_score + hl_score
    bearish = lh_score + ll_score

    # Also check last close vs midpoint of range
    range_high = max(h)
    range_low  = min(l)
    midpoint   = (range_high + range_low) / 2
    if c[-1] > midpoint:
        bullish += 1
    else:
        bearish += 1

    if bullish >= bearish + 2:
        strength = min(3, bullish - bearish)
        return "BULLISH", strength
    if bearish >= bullish + 2:
        strength = min(3, bearish - bullish)
        return "BEARISH", strength
    return "NEUTRAL", 0


def _compute_pd_zones(highs: list, lows: list, current_price: float, lookback: int = 50) -> dict:
    """
    Compute premium/discount zones based on the recent price range.
    Equilibrium = midpoint of (highest high, lowest low) over lookback bars.
    """
    n = min(lookback, len(highs))
    range_high = max(highs[-n:])
    range_low  = min(lows[-n:])
    equilibrium = (range_high + range_low) / 2

    in_premium  = current_price > equilibrium
    in_discount = current_price < equilibrium

    # Refined zones: top 25% = deep premium, bottom 25% = deep discount
    quarter = (range_high - range_low) / 4
    in_deep_premium  = current_price > (equilibrium + quarter)
    in_deep_discount = current_price < (equilibrium - quarter)

    return {
        "range_high":      round(range_high, 5),
        "range_low":       round(range_low, 5),
        "equilibrium":     round(equilibrium, 5),
        "in_premium":      in_premium,
        "in_discount":     in_discount,
        "in_deep_premium": in_deep_premium,
        "in_deep_discount":in_deep_discount,
    }


def _analyze_symbol(symbol_data: dict, config: dict, htf_tf: str = "15m") -> dict:
    """Compute context for a single symbol. Returns the 'context' sub-dict."""
    tf_data       = symbol_data.get("timeframes", {})
    current_price = symbol_data.get("current_price", 0.0)

    # Use trade_type HTF timeframe; fall back through available timeframes
    tf_htf = (tf_data.get(htf_tf) or tf_data.get("1h") or
              tf_data.get("15m") or tf_data.get("4h") or tf_data.get("1d"))

    if not tf_htf:
        return {
            "htf_bias": "NEUTRAL", "bias_strength": 0,
            "context_score": 0, "error": f"no_{htf_tf}_data",
        }

    highs  = tf_htf["highs"]
    lows   = tf_htf["lows"]
    closes = tf_htf["closes"]
    n_side = config.get("fractal_side_bars", 3)
    lookback = config.get("swing_lookback_bars", 50)

    # HTF Bias
    bias, strength = _detect_htf_bias(highs, lows, closes, lookback)

    # Swing highs/lows (liquidity zones)
    swing_highs, swing_lows = _find_swing_points(highs, lows, n_side)

    # Premium/Discount
    pd = _compute_pd_zones(highs, lows, current_price, lookback)

    # Context Score (0-100)
    score = 0

    # HTF bias: 40 pts (confirmed strength≥2), 20 pts (partial), 5 pts (NEUTRAL — ranging market)
    if bias == "BULLISH":
        score += 40 if strength >= 2 else 20
    elif bias == "BEARISH":
        score += 40 if strength >= 2 else 20
    else:
        score += 5  # NEUTRAL still gets a small credit — market hasn't broken structure

    # Price zone: 25 pts for deep zone, 15 pts for zone edge — awarded REGARDLESS of bias
    # (Price at an extreme is significant even in a ranging/NEUTRAL market)
    zone_score = 0
    if pd["in_deep_discount"] or pd["in_deep_premium"]:
        zone_score = 25
    elif pd["in_discount"] or pd["in_premium"]:
        zone_score = 15

    # Alignment bonus: bias direction matches the zone price is sitting in (+10)
    if (bias == "BULLISH" and pd["in_discount"]) or (bias == "BEARISH" and pd["in_premium"]):
        zone_score = min(30, zone_score + 10)

    score += zone_score

    # Liquidity zones nearby (within 1% of price): 30 pts
    # 1% covers ~$680 on BTC at 68k — appropriate for intraday/scalp proximity
    if current_price > 0:
        nearby_pct = config.get("liquidity_nearby_pct", 0.01)
        nearby = any(
            abs(lvl - current_price) / current_price < nearby_pct
            for lvl in swing_highs + swing_lows
        )
        if nearby:
            score += 30

    score = min(100, score)

    return {
        "htf_bias":      bias,
        "bias_strength": strength,
        "swing_highs":   [round(h, 5) for h in swing_highs],
        "swing_lows":    [round(l, 5) for l in swing_lows],
        "recent_swing_high": round(max(swing_highs), 5) if swing_highs else None,
        "recent_swing_low":  round(min(swing_lows), 5) if swing_lows else None,
        **pd,
        "context_score": score,
    }


class ContextBot:
    """Bot 2: Determines HTF bias, liquidity zones, premium/discount zones."""

    def run(self, pipeline_dict: dict) -> dict:
        config = load_config()
        symbols = pipeline_dict.get("symbols", {})
        processed = 0

        htf_tf = pipeline_dict.get("trade_type_config", {}).get("htf_tf", "15m")

        for sym, sym_data in symbols.items():
            if sym_data.get("fetch_status") != "ok":
                sym_data["context"] = {"htf_bias": "NEUTRAL", "context_score": 0, "skipped": True}
                continue
            try:
                sym_data["context"] = _analyze_symbol(sym_data, config, htf_tf)
                processed += 1
                logger.info(
                    f"  {sym}: bias={sym_data['context']['htf_bias']} "
                    f"score={sym_data['context']['context_score']}"
                )
            except Exception as e:
                logger.error(f"  {sym} context error: {e}")
                sym_data["context"] = {"htf_bias": "NEUTRAL", "context_score": 0, "error": str(e)}

        logger.info(f"Bot 2 complete: {processed} symbols analyzed")
        return pipeline_dict
