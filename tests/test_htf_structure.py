"""tests/test_htf_structure.py — Tests for detect_htf_structure() in institutional_detector."""

import pytest
from mcp_server.tools.institutional_detector import detect_htf_structure


# ── helpers ──────────────────────────────────────────────────────────


def _trending_up(n=40, start=100.0, step=1.0, noise=0.1):
    """Generate uptrending closes with slight noise."""
    import random; random.seed(42)
    c, h, l = [], [], []
    p = start
    for _ in range(n):
        p += step + random.uniform(-noise, noise)
        bar_range = abs(random.uniform(0.2, 0.8))
        h.append(p + bar_range)
        l.append(p - bar_range)
        c.append(p)
    return c, h, l


def _trending_down(n=40, start=100.0, step=1.0, noise=0.1):
    import random; random.seed(42)
    c, h, l = [], [], []
    p = start
    for _ in range(n):
        p -= step + random.uniform(-noise, noise)
        bar_range = abs(random.uniform(0.2, 0.8))
        h.append(p + bar_range)
        l.append(p - bar_range)
        c.append(p)
    return c, h, l


def _sideways(n=40, center=100.0, amplitude=2.0):
    import random; random.seed(7)
    import math
    c, h, l = [], [], []
    for i in range(n):
        p = center + amplitude * math.sin(i * 0.5) + random.uniform(-0.2, 0.2)
        c.append(p)
        h.append(p + 0.5)
        l.append(p - 0.5)
    return c, h, l


# ── basic direction tests ─────────────────────────────────────────────


def test_bullish_uptrend():
    c, h, l = _trending_up(n=50)
    assert detect_htf_structure(c, h, l) == "BULLISH"


def test_bearish_downtrend():
    c, h, l = _trending_down(n=50)
    assert detect_htf_structure(c, h, l) == "BEARISH"


def test_sideways_neutral():
    c, h, l = _sideways(n=40)
    result = detect_htf_structure(c, h, l)
    assert result == "NEUTRAL"


# ── fallback tests ────────────────────────────────────────────────────


def test_closes_only_uptrend():
    """Without highs/lows, should still work via closes proxy."""
    c, _, _ = _trending_up(n=40, step=2.0)
    assert detect_htf_structure(c) == "BULLISH"


def test_closes_only_downtrend():
    c, _, _ = _trending_down(n=40, step=2.0)
    assert detect_htf_structure(c) == "BEARISH"


def test_too_short_returns_neutral():
    assert detect_htf_structure([100, 101, 102]) == "NEUTRAL"


def test_exactly_15_bars_handled():
    c = list(range(100, 115))
    result = detect_htf_structure(c)
    assert result in ("BULLISH", "BEARISH", "NEUTRAL")


# ── edge cases ────────────────────────────────────────────────────────


def test_spike_does_not_fool_trend():
    """A single large spike in the first half should NOT flip a clear uptrend."""
    # Old algorithm: spike in first half makes max(first_half) high → appears bearish
    # New algorithm: uses swings, so spike is just one swing high, trend wins
    c, h, l = _trending_up(n=60, step=1.0)
    # Insert a big spike at bar 5
    h[5] = h[5] + 50
    c[5] = c[5] + 40
    result = detect_htf_structure(c, h, l)
    # Should still read BULLISH (post-spike trend) or NEUTRAL, NOT BEARISH
    assert result != "BEARISH"


def test_mismatched_highs_length_falls_back_to_closes():
    """If highs list length != closes, should use closes gracefully."""
    c, _, _ = _trending_up(n=40, step=1.5)
    wrong_h = [0.0, 1.0]  # wrong length
    # Should not crash; falls back to closes
    result = detect_htf_structure(c, wrong_h)
    assert result in ("BULLISH", "BEARISH", "NEUTRAL")


def test_nifty_like_uptrend():
    """Simulate NIFTY-style uptrend: gradual rise with daily noise."""
    import random; random.seed(99)
    base = 22000.0
    c, h, l = [], [], []
    for i in range(80):
        daily_move = random.uniform(-30, 60)  # biased up
        base += daily_move
        c.append(round(base, 2))
        h.append(round(base + random.uniform(10, 50), 2))
        l.append(round(base - random.uniform(10, 50), 2))
    assert detect_htf_structure(c, h, l) == "BULLISH"


def test_nifty_like_downtrend():
    """Simulate NIFTY-style sell-off."""
    import random; random.seed(99)
    base = 24000.0
    c, h, l = [], [], []
    for i in range(80):
        daily_move = random.uniform(-60, 20)  # biased down
        base += daily_move
        c.append(round(base, 2))
        h.append(round(base + random.uniform(10, 50), 2))
        l.append(round(base - random.uniform(10, 50), 2))
    assert detect_htf_structure(c, h, l) == "BEARISH"


def test_custom_lookback():
    """lookback parameter changes swing sensitivity."""
    c, h, l = _trending_up(n=60, step=1.0, noise=0.05)
    r1 = detect_htf_structure(c, h, l, lookback=2)
    r2 = detect_htf_structure(c, h, l, lookback=5)
    # Both should agree on a clean uptrend
    assert r1 == "BULLISH"
    assert r2 == "BULLISH"


def test_htf_weekly_daily_h4_all_bullish():
    """Simulate the 3-timeframe call pattern used in live/evening/scanner engines."""
    c, h, l = _trending_up(n=200, step=1.0, noise=0.2)
    def sl(arr, n): return arr[-n:] if len(arr) >= n else arr
    weekly = detect_htf_structure(sl(c, 100), sl(h, 100), sl(l, 100))
    daily  = detect_htf_structure(sl(c, 50),  sl(h, 50),  sl(l, 50))
    h4     = detect_htf_structure(sl(c, 20),  sl(h, 20),  sl(l, 20))
    assert weekly == "BULLISH"
    assert daily  == "BULLISH"
    assert h4     == "BULLISH"
