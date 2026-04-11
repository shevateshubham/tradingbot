"""
tools/options_analysis.py — Derives Max Pain, PCR, GEX, OI walls, IV skew
from raw NSE option chain data. Zero smoothing. Pure mathematical derivations.
"""

import logging
import math
from typing import Optional, Dict, List, Tuple, Any

logger = logging.getLogger(__name__)


# ── Black-Scholes for GEX calculation ─────────────────────────────

def _black_scholes_d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Calculate d1 from Black-Scholes model. Pure math."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x ** 2) / math.sqrt(2 * math.pi)


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via error function."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs_gamma(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """
    Black-Scholes Gamma: rate of change of delta per unit of underlying.
    Used for GEX (Gamma Exposure) calculation.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = _black_scholes_d1(S, K, T, r, sigma)
    return _norm_pdf(d1) / (S * sigma * math.sqrt(T))


def _bs_iv_approx(option_price: float, S: float, K: float, T: float, is_call: bool) -> float:
    """
    Approximate implied volatility using Brenner-Subrahmanyam approximation.
    Avoids iterative Newton-Raphson for speed. Accurate within ~2% for ATM.
    """
    if S <= 0 or K <= 0 or T <= 0 or option_price <= 0:
        return 0.0
    # Brenner-Subrahmanyam: IV ≈ option_price / (S * sqrt(T/2π)) * sqrt(2π)
    iv = option_price / S * math.sqrt(2 * math.pi / T)
    return max(0.001, min(iv, 5.0))  # Cap between 0.1% and 500%


# ── Max Pain Calculation ──────────────────────────────────────────

def calculate_max_pain(option_data: List[Dict]) -> Optional[float]:
    """
    Calculate Max Pain: the strike price where option writers lose the least.
    Algorithm: For each strike, sum total value of all in-the-money options.
    Max Pain = strike with minimum total loss to option writers.

    Args:
        option_data: List of strike records from NSE API records.data
    """
    if not option_data:
        return None

    # Collect all unique strikes
    strikes = sorted({
        float(record.get("strikePrice", 0))
        for record in option_data
        if record.get("strikePrice")
    })

    if not strikes:
        return None

    # For each potential expiry price, calculate total option writer loss
    min_pain = float("inf")
    max_pain_strike = strikes[0]

    for test_price in strikes:
        total_loss = 0.0

        for record in option_data:
            strike = float(record.get("strikePrice", 0))
            if strike <= 0:
                continue

            # Call OI: writers lose when price > strike
            ce_data = record.get("CE", {})
            ce_oi = float(ce_data.get("openInterest", 0) or 0)
            if test_price > strike:
                total_loss += ce_oi * (test_price - strike)

            # Put OI: writers lose when price < strike
            pe_data = record.get("PE", {})
            pe_oi = float(pe_data.get("openInterest", 0) or 0)
            if test_price < strike:
                total_loss += pe_oi * (strike - test_price)

        if total_loss < min_pain:
            min_pain = total_loss
            max_pain_strike = test_price

    logger.debug(f"Max Pain: {max_pain_strike} (total loss: {min_pain:,.0f})")
    return max_pain_strike


# ── PCR Calculation ───────────────────────────────────────────────

def calculate_pcr(option_data: List[Dict]) -> Optional[float]:
    """
    Put-Call Ratio: total put OI / total call OI.
    > 1.0 = more put buyers = bearish sentiment
    < 1.0 = more call buyers = bullish sentiment
    """
    total_put_oi = 0.0
    total_call_oi = 0.0

    for record in option_data:
        ce_data = record.get("CE", {})
        pe_data = record.get("PE", {})
        total_call_oi += float(ce_data.get("openInterest", 0) or 0)
        total_put_oi  += float(pe_data.get("openInterest", 0) or 0)

    if total_call_oi == 0:
        return None

    pcr = total_put_oi / total_call_oi
    logger.debug(f"PCR: {pcr:.3f} (P OI: {total_put_oi:,.0f}, C OI: {total_call_oi:,.0f})")
    return round(pcr, 3)


# ── OI Walls ──────────────────────────────────────────────────────

def calculate_oi_walls(option_data: List[Dict]) -> Dict[str, List[float]]:
    """
    Find top 3 call and put OI concentrations — these act as resistance/support.
    High CE OI = resistance wall. High PE OI = support wall.

    Returns:
        {
            "ce_walls": [strike1, strike2, strike3],   # resistance levels
            "pe_walls": [strike1, strike2, strike3],   # support levels
        }
    """
    ce_by_strike: Dict[float, float] = {}
    pe_by_strike: Dict[float, float] = {}

    for record in option_data:
        strike = float(record.get("strikePrice", 0))
        if strike <= 0:
            continue

        ce_data = record.get("CE", {})
        pe_data = record.get("PE", {})
        ce_oi = float(ce_data.get("openInterest", 0) or 0)
        pe_oi = float(pe_data.get("openInterest", 0) or 0)

        if ce_oi > 0:
            ce_by_strike[strike] = ce_oi
        if pe_oi > 0:
            pe_by_strike[strike] = pe_oi

    # Sort by OI descending, take top 3
    top_ce = sorted(ce_by_strike.keys(), key=lambda s: ce_by_strike[s], reverse=True)[:3]
    top_pe = sorted(pe_by_strike.keys(), key=lambda s: pe_by_strike[s], reverse=True)[:3]

    # Sort walls by strike price for readability
    ce_walls = sorted(top_ce)
    pe_walls = sorted(top_pe)

    logger.debug(f"CE walls (resistance): {ce_walls}")
    logger.debug(f"PE walls (support):    {pe_walls}")

    return {"ce_walls": ce_walls, "pe_walls": pe_walls}


# ── OI Change Bias ────────────────────────────────────────────────

def calculate_oi_bias(option_data: List[Dict], underlying_price: float) -> str:
    """
    Determine OI change direction: are more calls or puts being written?
    Based on net OI change (current vs previous fetch direction).

    Returns: "BULLISH", "BEARISH", or "NEUTRAL"
    """
    atm_range = underlying_price * 0.03  # ±3% range around ATM
    put_oi_change = 0.0
    call_oi_change = 0.0

    for record in option_data:
        strike = float(record.get("strikePrice", 0))
        if not (underlying_price - atm_range <= strike <= underlying_price + atm_range):
            continue

        ce_data = record.get("CE", {})
        pe_data = record.get("PE", {})
        ce_change = float(ce_data.get("changeinOpenInterest", 0) or 0)
        pe_change = float(pe_data.get("changeinOpenInterest", 0) or 0)

        call_oi_change += ce_change
        put_oi_change  += pe_change

    if put_oi_change > call_oi_change * 1.2:
        return "BEARISH"  # Put writers active = expect downside protection needed
    elif call_oi_change > put_oi_change * 1.2:
        return "BULLISH"  # Call writers active = expect upside resistance
    else:
        return "NEUTRAL"


# ── GEX Calculation ───────────────────────────────────────────────

def calculate_gex(
    option_data: List[Dict],
    underlying_price: float,
    days_to_expiry: int = 7,
    lot_size: int = 50,
) -> float:
    """
    Net Gamma Exposure: net dealer gamma across all strikes.
    Positive GEX = dealers are long gamma = market stabilizing.
    Negative GEX = dealers are short gamma = market amplifying moves.

    Args:
        option_data:      NSE records data list
        underlying_price: Current spot price
        days_to_expiry:   Days until expiry (default: 7 for weekly)
        lot_size:         Contract lot size (NIFTY=50, BANKNIFTY=15, FINNIFTY=40)
    """
    T = days_to_expiry / 365.0
    r = 0.065  # Indian risk-free rate ~6.5%
    total_gex = 0.0

    for record in option_data:
        strike = float(record.get("strikePrice", 0))
        if strike <= 0:
            continue

        ce_data = record.get("CE", {})
        pe_data = record.get("PE", {})

        ce_oi = float(ce_data.get("openInterest", 0) or 0)
        pe_oi = float(pe_data.get("openInterest", 0) or 0)

        # Approximate IV from last price if available
        ce_ltp = float(ce_data.get("lastPrice", 0) or 0)
        pe_ltp = float(pe_data.get("lastPrice", 0) or 0)

        ce_iv = _bs_iv_approx(ce_ltp, underlying_price, strike, T, is_call=True) if ce_ltp > 0 else 0.2
        pe_iv = _bs_iv_approx(pe_ltp, underlying_price, strike, T, is_call=False) if pe_ltp > 0 else 0.2

        ce_gamma = _bs_gamma(underlying_price, strike, T, r, ce_iv) if ce_iv > 0 else 0
        pe_gamma = _bs_gamma(underlying_price, strike, T, r, pe_iv) if pe_iv > 0 else 0

        # Dealers are short calls (negative gamma) and long puts (negative gamma)
        total_gex += (ce_oi - pe_oi) * ce_gamma * underlying_price * lot_size

    return round(total_gex, 2)


# ── IV Skew ───────────────────────────────────────────────────────

def calculate_iv_skew(option_data: List[Dict], underlying_price: float) -> str:
    """
    IV Skew direction: compare put IV vs call IV around ATM.
    Put IV > Call IV = bearish skew (demand for downside protection)
    Call IV > Put IV = bullish skew (demand for upside participation)
    """
    atm_range = underlying_price * 0.02
    put_ivs = []
    call_ivs = []

    for record in option_data:
        strike = float(record.get("strikePrice", 0))
        if not (underlying_price - atm_range <= strike <= underlying_price + atm_range):
            continue

        ce_data = record.get("CE", {})
        pe_data = record.get("PE", {})
        ce_iv = float(ce_data.get("impliedVolatility", 0) or 0)
        pe_iv = float(pe_data.get("impliedVolatility", 0) or 0)

        if ce_iv > 0:
            call_ivs.append(ce_iv)
        if pe_iv > 0:
            put_ivs.append(pe_iv)

    if not put_ivs or not call_ivs:
        return "NEUTRAL"

    avg_put_iv  = sum(put_ivs) / len(put_ivs)
    avg_call_iv = sum(call_ivs) / len(call_ivs)

    if avg_put_iv > avg_call_iv * 1.05:
        return "BEARISH_SKEW"
    elif avg_call_iv > avg_put_iv * 1.05:
        return "BULLISH_SKEW"
    else:
        return "NEUTRAL"


# ── Master analysis function ──────────────────────────────────────

def analyze_option_chain(
    raw_data: Dict[str, Any],
    underlying_price: float,
    days_to_expiry: int = 7,
    lot_size: int = 50,
) -> Dict[str, Any]:
    """
    Run full options analysis on raw NSE response.
    Returns dict with all metrics for the signal scorer.
    """
    if not raw_data or "records" not in raw_data:
        return {}

    records = raw_data["records"]
    option_data = records.get("data", [])
    underlying_value = float(records.get("underlyingValue", underlying_price) or underlying_price)

    if not option_data:
        logger.warning("No option data in NSE response")
        return {}

    try:
        max_pain    = calculate_max_pain(option_data)
        pcr         = calculate_pcr(option_data)
        oi_walls    = calculate_oi_walls(option_data)
        oi_bias     = calculate_oi_bias(option_data, underlying_value)
        gex         = calculate_gex(option_data, underlying_value, days_to_expiry, lot_size=lot_size)
        iv_skew     = calculate_iv_skew(option_data, underlying_value)

        result = {
            "max_pain":         max_pain,
            "pcr":              pcr,
            "ce_walls":         oi_walls.get("ce_walls", []),
            "pe_walls":         oi_walls.get("pe_walls", []),
            "oi_bias":          oi_bias,
            "gex":              gex,
            "iv_skew":          iv_skew,
            "underlying_value": underlying_value,
        }

        # Determine overall options direction bias
        bullish_signals = sum([
            pcr is not None and pcr > 1.2,         # High put OI = market expects support
            oi_bias == "BULLISH",
            iv_skew == "BULLISH_SKEW",
            gex > 0,                                # Positive GEX = stabilizing
        ])
        bearish_signals = sum([
            pcr is not None and pcr < 0.8,         # Low put OI = market complacent
            oi_bias == "BEARISH",
            iv_skew == "BEARISH_SKEW",
            gex < 0,                                # Negative GEX = amplifying moves
        ])

        if bullish_signals >= 3:
            result["options_direction"] = "BULLISH"
        elif bearish_signals >= 3:
            result["options_direction"] = "BEARISH"
        else:
            result["options_direction"] = "NEUTRAL"

        logger.info(
            f"Options analysis: max_pain={max_pain} pcr={pcr:.3f} "
            f"oi_bias={oi_bias} gex={gex:.0f} dir={result['options_direction']}"
        )
        return result

    except Exception as e:
        logger.error(f"Options analysis error: {e}", exc_info=True)
        return {}


def is_near_max_pain(current_price: float, max_pain: float, threshold_pct: float = 0.5) -> bool:
    """Check if price is within threshold_pct% of max pain level."""
    if not max_pain or not current_price:
        return False
    pct_diff = abs(current_price - max_pain) / current_price * 100
    return pct_diff <= threshold_pct
