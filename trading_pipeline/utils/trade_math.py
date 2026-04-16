"""
utils/trade_math.py — Entry, SL, TP, RR calculation.
Ported from mcp_server/tools/trade_calculator.py (lines 72-153).
Pure math, no external dependencies.
"""

from __future__ import annotations


def _avoid_round_number(price: float, direction: str, buffer_pct: float = 0.0005) -> float:
    """
    Nudge SL slightly away from round numbers to avoid stop hunts.
    Ported from trade_calculator.py:72-89.
    """
    round_levels = [
        round(price, -4),
        round(price, -3),
        round(price, -2),
    ]
    for level in round_levels:
        if level != 0 and abs(price - level) / price < 0.001:
            if direction == "LONG":
                price -= price * buffer_pct
            else:
                price += price * buffer_pct
            break
    return round(price, 5)


def calculate_levels(
    ob_high: float,
    ob_low: float,
    direction: str,
    fvg_high: float | None = None,
    fvg_low: float | None = None,
) -> dict:
    """
    Derive entry, SL, TP1, TP2, TP3 from OB/FVG levels.
    Ported from trade_calculator.py:92-153.

    Returns dict with entry, sl, tp1, tp2, tp3, sl_points, sl_percent, rr_ratio.
    """
    ob_mid = (ob_high + ob_low) / 2

    # Entry: OB+FVG overlap midpoint if available, else OB midpoint
    if fvg_high is not None and fvg_low is not None:
        overlap_high = min(ob_high, fvg_high)
        overlap_low  = max(ob_low, fvg_low)
        if overlap_high > overlap_low:
            entry = (overlap_high + overlap_low) / 2
        else:
            entry = ob_mid
    else:
        entry = ob_mid

    if entry <= 0:
        entry = ob_mid

    sl_buffer = 0.0005

    if direction == "LONG":
        raw_sl = ob_low * (1 - sl_buffer)
        sl = _avoid_round_number(raw_sl, "LONG")
    else:
        raw_sl = ob_high * (1 + sl_buffer)
        sl = _avoid_round_number(raw_sl, "SHORT")

    sl = round(sl, 5)
    sl_points  = abs(entry - sl)
    sl_percent = (sl_points / entry * 100) if entry > 0 else 0

    if direction == "LONG":
        tp1 = round(entry + sl_points * 1.0, 5)
        tp2 = round(entry + sl_points * 2.0, 5)
        tp3 = round(entry + sl_points * 3.0, 5)
    else:
        tp1 = round(entry - sl_points * 1.0, 5)
        tp2 = round(entry - sl_points * 2.0, 5)
        tp3 = round(entry - sl_points * 3.0, 5)

    rr_ratio = round(abs(tp2 - entry) / sl_points, 2) if sl_points > 0 else 0

    return {
        "entry":      round(entry, 5),
        "sl":         sl,
        "tp1":        tp1,
        "tp2":        tp2,
        "tp3":        tp3,
        "sl_points":  round(sl_points, 5),
        "sl_percent": round(sl_percent, 4),
        "rr_ratio":   rr_ratio,
    }


def validate_trade(levels: dict, max_sl_percent: float = 2.0, min_rr_ratio: float = 2.0) -> tuple[bool, str | None]:
    """
    Hard discard checks: SL too wide or RR too low.
    Returns (is_valid, reject_reason).
    """
    if levels["sl_percent"] > max_sl_percent:
        return False, f"sl_too_wide:{levels['sl_percent']:.2f}%>max_{max_sl_percent}%"
    if levels["rr_ratio"] < min_rr_ratio - 0.001:
        return False, f"rr_too_low:{levels['rr_ratio']:.2f}<{min_rr_ratio}"
    return True, None


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """Average True Range — used to measure impulse magnitude."""
    if len(highs) < period + 1:
        return 0.0
    trs = []
    for i in range(1, min(period + 1, len(highs))):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0
