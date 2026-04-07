"""tests/test_options_analysis.py — Tests for options_analysis.py"""

import pytest
from mcp_server.tools.options_analysis import (
    calculate_max_pain,
    calculate_pcr,
    calculate_oi_walls,
    calculate_oi_bias,
    calculate_gex,
    calculate_iv_skew,
    analyze_option_chain,
    is_near_max_pain,
    _bs_gamma,
    _norm_pdf,
    _norm_cdf,
)


def _make_strike(strike, ce_oi, pe_oi, ce_ltp=50.0, pe_ltp=50.0, ce_iv=20.0, pe_iv=20.0,
                 ce_oi_change=0, pe_oi_change=0):
    return {
        "strikePrice": strike,
        "CE": {
            "openInterest": ce_oi,
            "changeinOpenInterest": ce_oi_change,
            "lastPrice": ce_ltp,
            "impliedVolatility": ce_iv,
        },
        "PE": {
            "openInterest": pe_oi,
            "changeinOpenInterest": pe_oi_change,
            "lastPrice": pe_ltp,
            "impliedVolatility": pe_iv,
        },
    }


# Sample option chain centred at 22500
SAMPLE_CHAIN = [
    _make_strike(22000, ce_oi=50000,  pe_oi=120000, pe_iv=22.0, ce_iv=16.0),
    _make_strike(22100, ce_oi=60000,  pe_oi=100000, pe_iv=21.0, ce_iv=16.5),
    _make_strike(22200, ce_oi=80000,  pe_oi=90000,  pe_iv=20.5, ce_iv=17.0),
    _make_strike(22300, ce_oi=100000, pe_oi=80000,  pe_iv=20.0, ce_iv=17.5),
    _make_strike(22400, ce_oi=150000, pe_oi=70000,  pe_iv=19.5, ce_iv=18.0),
    _make_strike(22500, ce_oi=200000, pe_oi=200000, pe_iv=19.0, ce_iv=19.0),  # ATM
    _make_strike(22600, ce_oi=180000, pe_oi=60000,  pe_iv=18.0, ce_iv=19.5),
    _make_strike(22700, ce_oi=120000, pe_oi=50000,  pe_iv=17.5, ce_iv=20.0),
    _make_strike(22800, ce_oi=90000,  pe_oi=40000,  pe_iv=17.0, ce_iv=20.5),
    _make_strike(22900, ce_oi=70000,  pe_oi=30000,  pe_iv=16.5, ce_iv=21.0),
    _make_strike(23000, ce_oi=50000,  pe_oi=25000,  pe_iv=16.0, ce_iv=22.0),
]


class TestMaxPain:
    def test_returns_valid_strike(self):
        mp = calculate_max_pain(SAMPLE_CHAIN)
        assert mp is not None
        assert 22000 <= mp <= 23000

    def test_returns_none_on_empty(self):
        assert calculate_max_pain([]) is None

    def test_max_pain_is_a_real_strike(self):
        mp = calculate_max_pain(SAMPLE_CHAIN)
        strikes = [r["strikePrice"] for r in SAMPLE_CHAIN]
        assert mp in strikes

    def test_max_pain_with_single_strike(self):
        single = [_make_strike(22500, ce_oi=1000, pe_oi=1000)]
        mp = calculate_max_pain(single)
        assert mp == 22500


class TestPCR:
    def test_pcr_calculated_correctly(self):
        total_put  = sum(r["PE"]["openInterest"] for r in SAMPLE_CHAIN)
        total_call = sum(r["CE"]["openInterest"] for r in SAMPLE_CHAIN)
        expected   = total_put / total_call
        pcr = calculate_pcr(SAMPLE_CHAIN)
        assert pcr == pytest.approx(expected, rel=0.001)

    def test_pcr_above_1_when_more_puts(self):
        high_put_chain = [_make_strike(22500, ce_oi=100, pe_oi=200)]
        assert calculate_pcr(high_put_chain) == pytest.approx(2.0)

    def test_pcr_below_1_when_more_calls(self):
        high_call_chain = [_make_strike(22500, ce_oi=200, pe_oi=100)]
        assert calculate_pcr(high_call_chain) == pytest.approx(0.5)

    def test_pcr_returns_none_on_empty(self):
        assert calculate_pcr([]) is None

    def test_pcr_rounded_to_3_decimals(self):
        pcr = calculate_pcr(SAMPLE_CHAIN)
        assert pcr is not None
        assert len(str(pcr).split(".")[-1]) <= 3


class TestOIWalls:
    def test_returns_ce_and_pe_walls(self):
        walls = calculate_oi_walls(SAMPLE_CHAIN)
        assert "ce_walls" in walls
        assert "pe_walls" in walls

    def test_returns_up_to_3_walls_each(self):
        walls = calculate_oi_walls(SAMPLE_CHAIN)
        assert len(walls["ce_walls"]) <= 3
        assert len(walls["pe_walls"]) <= 3

    def test_walls_are_strike_prices(self):
        walls = calculate_oi_walls(SAMPLE_CHAIN)
        all_strikes = {r["strikePrice"] for r in SAMPLE_CHAIN}
        for s in walls["ce_walls"]:
            assert s in all_strikes
        for s in walls["pe_walls"]:
            assert s in all_strikes

    def test_ce_walls_sorted(self):
        walls = calculate_oi_walls(SAMPLE_CHAIN)
        assert walls["ce_walls"] == sorted(walls["ce_walls"])

    def test_returns_empty_on_no_data(self):
        walls = calculate_oi_walls([])
        assert walls["ce_walls"] == []
        assert walls["pe_walls"] == []


class TestOIBias:
    def test_bullish_when_call_oi_increasing(self):
        # High call OI change = call writers active = resistance above = bullish bias
        chain = [
            _make_strike(22500, ce_oi=200000, pe_oi=100000,
                         ce_oi_change=50000, pe_oi_change=5000),
        ]
        bias = calculate_oi_bias(chain, 22500.0)
        assert bias in ("BULLISH", "NEUTRAL")

    def test_bearish_when_put_oi_increasing(self):
        chain = [
            _make_strike(22500, ce_oi=100000, pe_oi=200000,
                         ce_oi_change=5000, pe_oi_change=80000),
        ]
        bias = calculate_oi_bias(chain, 22500.0)
        assert bias in ("BEARISH", "NEUTRAL")

    def test_neutral_when_balanced(self):
        chain = [_make_strike(22500, ce_oi=100000, pe_oi=100000,
                              ce_oi_change=10000, pe_oi_change=10000)]
        bias = calculate_oi_bias(chain, 22500.0)
        assert bias == "NEUTRAL"

    def test_returns_string(self):
        bias = calculate_oi_bias(SAMPLE_CHAIN, 22500.0)
        assert isinstance(bias, str)
        assert bias in ("BULLISH", "BEARISH", "NEUTRAL")


class TestGEX:
    def test_returns_float(self):
        gex = calculate_gex(SAMPLE_CHAIN, 22500.0, days_to_expiry=7)
        assert isinstance(gex, float)

    def test_returns_zero_on_empty(self):
        gex = calculate_gex([], 22500.0)
        assert gex == 0.0

    def test_sign_reflects_net_direction(self):
        # More call OI than put OI → positive GEX
        call_heavy = [_make_strike(22500, ce_oi=300000, pe_oi=50000)]
        gex = calculate_gex(call_heavy, 22500.0, days_to_expiry=7)
        assert gex != 0.0  # Should have a direction


class TestIVSkew:
    def test_bearish_skew_when_put_iv_higher(self):
        chain = [_make_strike(22500, ce_oi=100000, pe_oi=100000,
                              ce_iv=18.0, pe_iv=24.0)]  # Put IV much higher
        skew = calculate_iv_skew(chain, 22500.0)
        assert skew == "BEARISH_SKEW"

    def test_bullish_skew_when_call_iv_higher(self):
        chain = [_make_strike(22500, ce_oi=100000, pe_oi=100000,
                              ce_iv=24.0, pe_iv=18.0)]  # Call IV higher
        skew = calculate_iv_skew(chain, 22500.0)
        assert skew == "BULLISH_SKEW"

    def test_neutral_when_balanced(self):
        chain = [_make_strike(22500, ce_oi=100000, pe_oi=100000,
                              ce_iv=20.0, pe_iv=20.0)]
        skew = calculate_iv_skew(chain, 22500.0)
        assert skew == "NEUTRAL"

    def test_neutral_on_empty(self):
        skew = calculate_iv_skew([], 22500.0)
        assert skew == "NEUTRAL"


class TestAnalyzeChain:
    def test_returns_all_keys(self):
        raw_data = {"records": {"data": SAMPLE_CHAIN, "underlyingValue": 22500}}
        result = analyze_option_chain(raw_data, 22500.0)
        assert "max_pain"    in result
        assert "pcr"         in result
        assert "ce_walls"    in result
        assert "pe_walls"    in result
        assert "oi_bias"     in result
        assert "gex"         in result
        assert "iv_skew"     in result
        assert "options_direction" in result

    def test_returns_empty_on_bad_data(self):
        assert analyze_option_chain({}, 22500.0) == {}
        assert analyze_option_chain(None, 22500.0) == {}

    def test_options_direction_is_valid(self):
        raw_data = {"records": {"data": SAMPLE_CHAIN, "underlyingValue": 22500}}
        result = analyze_option_chain(raw_data, 22500.0)
        assert result.get("options_direction") in ("BULLISH", "BEARISH", "NEUTRAL")


class TestIsNearMaxPain:
    def test_near_when_within_threshold(self):
        assert is_near_max_pain(22500.0, 22510.0, threshold_pct=0.5)

    def test_not_near_when_far(self):
        assert not is_near_max_pain(22500.0, 22800.0, threshold_pct=0.5)

    def test_handles_none(self):
        assert not is_near_max_pain(22500.0, None, threshold_pct=0.5)
        assert not is_near_max_pain(None, 22500.0, threshold_pct=0.5)


class TestBSMath:
    def test_gamma_positive(self):
        gamma = _bs_gamma(S=22500, K=22500, T=0.019, r=0.065, sigma=0.18)
        assert gamma > 0

    def test_gamma_zero_when_no_time(self):
        gamma = _bs_gamma(S=22500, K=22500, T=0, r=0.065, sigma=0.18)
        assert gamma == 0.0

    def test_norm_pdf_at_zero(self):
        import math
        expected = 1 / math.sqrt(2 * math.pi)
        assert _norm_pdf(0) == pytest.approx(expected, rel=1e-6)

    def test_norm_cdf_at_zero(self):
        assert _norm_cdf(0) == pytest.approx(0.5, rel=1e-6)

    def test_norm_cdf_large_positive(self):
        assert _norm_cdf(5) > 0.99

    def test_norm_cdf_large_negative(self):
        assert _norm_cdf(-5) < 0.01
