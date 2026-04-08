"""
institutional_detector.py — SMC institutional activity analysis.
Detects Order Blocks, FVGs, Liquidity Events, Breaker Blocks, Wyckoff phases.
"""
from enum import Enum
from dataclasses import dataclass, field
from typing import List


class LiquidityEvent(Enum):
    NONE = "NONE"
    SSL_SWEPT = "SSL_SWEPT"          # Sell-side liquidity swept (lows taken out)
    BSL_SWEPT = "BSL_SWEPT"          # Buy-side liquidity swept (highs taken out)
    IND_BULL = "IND_BULL"            # Inducement — fake bearish push before bull move
    IND_BEAR = "IND_BEAR"            # Inducement — fake bullish push before bear move
    TURTLE_BULL = "TURTLE_BULL"      # Turtle trap — false breakdown below low → reversal up
    TURTLE_BEAR = "TURTLE_BEAR"      # Turtle trap — false breakout above high → reversal down


class WyckoffPhase(Enum):
    ACCUMULATION = "ACCUMULATION"
    DISTRIBUTION = "DISTRIBUTION"
    REACCUMULATION = "REACCUMULATION"
    REDISTRIBUTION = "REDISTRIBUTION"
    MARKUP = "MARKUP"
    MARKDOWN = "MARKDOWN"
    UNKNOWN = "UNKNOWN"


@dataclass
class InstitutionalAnalysis:
    institutional_bias: str          # "BULLISH" | "BEARISH" | "NEUTRAL"
    total_score: float
    liquidity_event: LiquidityEvent
    breaker_block: bool              # Failed OB that caused BOS
    propulsion_block: bool           # OB + FVG overlap (strong confluence)
    mitigation_block: bool           # OB already retested/touched
    wyckoff_phase: WyckoffPhase
    evidence: List[str] = field(default_factory=list)


def _detect_fvg(highs: list, lows: list, closes: list, direction: str) -> bool:
    """
    Fair Value Gap: 3-candle pattern.
    Bullish FVG: lows[i] > highs[i-2]   (gap up — unfilled space)
    Bearish FVG: highs[i] < lows[i-2]   (gap down — unfilled space)
    Checks the last 10 candles.
    """
    n = len(closes)
    if n < 3:
        return False
    for i in range(max(2, n - 10), n):
        if direction == "BULLISH" and lows[i] > highs[i - 2]:
            return True
        if direction == "BEARISH" and highs[i] < lows[i - 2]:
            return True
    return False


def _detect_order_block(
    opens: list, highs: list, lows: list, closes: list, direction: str
) -> tuple:
    """
    Returns (ob_found: bool, already_mitigated: bool).

    Bullish OB: last bearish candle before a strong bullish impulse.
    Bearish OB: last bullish candle before a strong bearish impulse.
    Mitigation: current price has revisited the OB zone.
    """
    n = len(closes)
    if n < 5:
        return False, False

    cur = closes[-1]

    if direction == "BULLISH":
        for i in range(n - 5, max(0, n - 30), -1):
            if closes[i] < opens[i]:                          # bearish candle
                if i + 2 < n and closes[i + 2] > highs[i]:   # strong up move after
                    ob_high = highs[i]
                    ob_low = lows[i]
                    already_touched = ob_low <= cur <= ob_high
                    return True, already_touched
    else:
        for i in range(n - 5, max(0, n - 30), -1):
            if closes[i] > opens[i]:                          # bullish candle
                if i + 2 < n and closes[i + 2] < lows[i]:    # strong down move after
                    ob_high = highs[i]
                    ob_low = lows[i]
                    already_touched = ob_low <= cur <= ob_high
                    return True, already_touched

    return False, False


def _detect_breaker_block(highs: list, lows: list, closes: list, direction: str) -> bool:
    """
    Breaker Block: a failed OB zone that price swept through, then reclaimed (BOS).
    Bullish breaker: price breaks below a prior swing low then reclaims above it.
    Bearish breaker: price breaks above a prior swing high then falls back below it.
    """
    n = len(closes)
    if n < 10:
        return False

    if direction == "BULLISH":
        recent_low = min(lows[-10:-3])
        if lows[-3] < recent_low and closes[-1] > recent_low:
            return True
    else:
        recent_high = max(highs[-10:-3])
        if highs[-3] > recent_high and closes[-1] < recent_high:
            return True

    return False


def _detect_liquidity_event(
    highs: list, lows: list, closes: list,
    swing_low: float, swing_high: float,
) -> LiquidityEvent:
    """Determine the most recent liquidity event from price action."""
    n = len(closes)
    if n < 5:
        return LiquidityEvent.NONE

    cur = closes[-1]
    lo3 = min(lows[-3:])
    hi3 = max(highs[-3:])
    ref_close = closes[-4] if n >= 4 else closes[-2]

    # SSL swept: dipped below swing_low then closed above → bearish lows hunted
    if lo3 < swing_low and cur > swing_low:
        return LiquidityEvent.TURTLE_BULL if cur > ref_close else LiquidityEvent.SSL_SWEPT

    # BSL swept: spiked above swing_high then closed below → bullish highs hunted
    if hi3 > swing_high and cur < swing_high:
        return LiquidityEvent.TURTLE_BEAR if cur < ref_close else LiquidityEvent.BSL_SWEPT

    # Inducement: minor false push against prevailing direction
    if n >= 6:
        mid = sum(closes[-6:]) / 6
        if cur > mid and lows[-1] < lows[-3]:
            return LiquidityEvent.IND_BULL
        if cur < mid and highs[-1] > highs[-3]:
            return LiquidityEvent.IND_BEAR

    return LiquidityEvent.NONE


def _detect_wyckoff(closes: list, volumes: list) -> WyckoffPhase:
    """Simplified Wyckoff phase detection based on price structure and volume."""
    n = len(closes)
    if n < 20:
        return WyckoffPhase.UNKNOWN

    mid = n // 2
    vol_first = sum(volumes[:mid]) / mid
    vol_second = sum(volumes[mid:]) / (n - mid)
    vol_expanding = vol_second > vol_first * 1.1
    vol_contracting = vol_second < vol_first * 0.9

    high_first, low_first = max(closes[:mid]), min(closes[:mid])
    high_second, low_second = max(closes[mid:]), min(closes[mid:])

    trending_up = high_second > high_first and low_second > low_first
    trending_down = high_second < high_first and low_second < low_first
    range_mid = (max(closes) + min(closes)) / 2

    if trending_up and vol_expanding:
        return WyckoffPhase.MARKUP
    if trending_down and vol_expanding:
        return WyckoffPhase.MARKDOWN
    if not trending_up and not trending_down and vol_contracting:
        return WyckoffPhase.ACCUMULATION if closes[-1] < range_mid else WyckoffPhase.DISTRIBUTION
    if trending_up and vol_contracting:
        return WyckoffPhase.REACCUMULATION
    if trending_down and vol_contracting:
        return WyckoffPhase.REDISTRIBUTION

    return WyckoffPhase.UNKNOWN


def analyze_institutional_activity(
    opens: list,
    highs: list,
    lows: list,
    closes: list,
    volumes: list,
    swing_low: float,
    swing_high: float,
    weekly_trend: str,
) -> InstitutionalAnalysis:
    """
    Analyze OHLCV data for institutional SMC activity.

    Parameters
    ----------
    opens, highs, lows, closes, volumes : list of float
    swing_low   : recent 20-bar swing low
    swing_high  : recent 20-bar swing high
    weekly_trend: "BULLISH" | "BEARISH" | "NEUTRAL"

    Returns
    -------
    InstitutionalAnalysis
    """
    evidence: List[str] = []
    score = 0.0
    n = len(closes)

    if n < 5:
        return InstitutionalAnalysis(
            institutional_bias="NEUTRAL",
            total_score=0.0,
            liquidity_event=LiquidityEvent.NONE,
            breaker_block=False,
            propulsion_block=False,
            mitigation_block=False,
            wyckoff_phase=WyckoffPhase.UNKNOWN,
            evidence=["Insufficient data"],
        )

    # ── 1. Raw directional bias from recent closes ─────────────────────
    lookback = min(10, n)
    price_change = closes[-1] - closes[-lookback]
    pct_change = abs(price_change) / (closes[-lookback] + 1e-9)
    if pct_change < 0.001:
        raw_bias = "NEUTRAL"
    elif price_change > 0:
        raw_bias = "BULLISH"
    else:
        raw_bias = "BEARISH"

    direction = raw_bias if raw_bias != "NEUTRAL" else (weekly_trend if weekly_trend != "NEUTRAL" else "BULLISH")

    # ── 2. Order Block ─────────────────────────────────────────────────
    ob_found, mitigation_block = _detect_order_block(opens, highs, lows, closes, direction)
    if ob_found:
        score += 20
        evidence.append(f"{'Bullish' if direction == 'BULLISH' else 'Bearish'} OB detected (+20)")
    if mitigation_block:
        score -= 8
        evidence.append("OB already mitigated (penalty -8)")

    # ── 3. FVG ────────────────────────────────────────────────────────
    fvg = _detect_fvg(highs, lows, closes, direction)
    propulsion_block = ob_found and fvg
    if fvg:
        bonus = 18 if propulsion_block else 8
        score += bonus
        evidence.append(f"FVG present{' — OB+FVG confluence' if propulsion_block else ''} (+{bonus})")

    # ── 4. Breaker Block ───────────────────────────────────────────────
    breaker_block = _detect_breaker_block(highs, lows, closes, direction)
    if breaker_block:
        score += 15
        evidence.append("Breaker block confirmed — BOS (+15)")

    # ── 5. Liquidity Event ─────────────────────────────────────────────
    liquidity_event = _detect_liquidity_event(highs, lows, closes, swing_low, swing_high)
    if liquidity_event != LiquidityEvent.NONE:
        score += 10
        evidence.append(f"Liquidity event: {liquidity_event.value} (+10)")

    # ── 6. Volume spike ────────────────────────────────────────────────
    if len(volumes) >= 5:
        avg_vol = sum(volumes[-20:]) / min(20, len(volumes))
        if avg_vol > 0 and volumes[-1] > avg_vol * 1.5:
            score += 5
            evidence.append("Volume spike — institutional participation (+5)")

    # ── 7. HTF alignment ───────────────────────────────────────────────
    if weekly_trend != "NEUTRAL":
        if direction == weekly_trend:
            score += 10
            evidence.append(f"Aligned with weekly trend: {weekly_trend} (+10)")
        else:
            score -= 10
            evidence.append(f"Counter-trend vs weekly {weekly_trend} (penalty -10)")

    # ── 8. Wyckoff phase ───────────────────────────────────────────────
    wyckoff_phase = _detect_wyckoff(closes, volumes)
    bullish_phases = (WyckoffPhase.ACCUMULATION, WyckoffPhase.MARKUP, WyckoffPhase.REACCUMULATION)
    bearish_phases = (WyckoffPhase.DISTRIBUTION, WyckoffPhase.MARKDOWN, WyckoffPhase.REDISTRIBUTION)
    if (direction == "BULLISH" and wyckoff_phase in bullish_phases) or \
       (direction == "BEARISH" and wyckoff_phase in bearish_phases):
        score += 8
        evidence.append(f"Wyckoff: {wyckoff_phase.value} (+8)")
    else:
        evidence.append(f"Wyckoff: {wyckoff_phase.value}")

    # ── 9. Final bias ──────────────────────────────────────────────────
    score = max(0.0, min(100.0, score))
    if score >= 15:
        institutional_bias = direction
    else:
        institutional_bias = "NEUTRAL"

    return InstitutionalAnalysis(
        institutional_bias=institutional_bias,
        total_score=round(score, 1),
        liquidity_event=liquidity_event,
        breaker_block=breaker_block,
        propulsion_block=propulsion_block,
        mitigation_block=mitigation_block,
        wyckoff_phase=wyckoff_phase,
        evidence=evidence,
    )
