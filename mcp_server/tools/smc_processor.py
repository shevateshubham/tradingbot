"""
tools/smc_processor.py — Enriches normalized webhook payload.
Fetches options chain data for eligible segments.
Validates SMC setup consistency.
Prepares full context for the scorer.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import pytz

from mcp_server.config import (
    get_settings, SESSIONS, KILLZONE_MINUTES,
    INDEX_SYMBOLS, FNO_SYMBOLS,
)
from mcp_server.tools.webhook_handler import NormalizedPayload
from mcp_server.resources.feeds import fetch_nse_option_chain
from mcp_server.tools.options_analysis import analyze_option_chain, is_near_max_pain
from mcp_server.tools.segment_detector import get_base_symbol

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ── Session detection ─────────────────────────────────────────────

def detect_active_session(dt_ist: datetime) -> Optional[str]:
    """Return the active session name for a given IST datetime."""
    current_minutes = dt_ist.hour * 60 + dt_ist.minute

    for session_name, times in SESSIONS.items():
        start_h, start_m = map(int, times["start"].split(":"))
        end_h, end_m     = map(int, times["end"].split(":"))
        start_min = start_h * 60 + start_m
        end_min   = end_h * 60 + end_m
        if start_min <= current_minutes <= end_min:
            return session_name

    return None


def detect_killzone(dt_ist: datetime) -> bool:
    """Return True if IST time is within ±30 min of any session open."""
    current_minutes = dt_ist.hour * 60 + dt_ist.minute

    for session_name, times in SESSIONS.items():
        open_h, open_m = map(int, times["start"].split(":"))
        open_min = open_h * 60 + open_m
        diff = abs(current_minutes - open_min)
        diff = min(diff, 1440 - diff)  # Handle midnight wrap
        if diff <= KILLZONE_MINUTES:
            return True
    return False


def is_lunch_hour(dt_ist: datetime) -> bool:
    """True if within 12:00–13:30 IST (Indian market lunch slump)."""
    mins = dt_ist.hour * 60 + dt_ist.minute
    return 12 * 60 <= mins <= 13 * 60 + 30


def is_indian_market_open(dt_ist: datetime) -> bool:
    """True if Indian market is open (9:15 AM – 3:30 PM, Mon–Fri)."""
    if dt_ist.weekday() >= 5:  # Saturday or Sunday
        return False
    mins = dt_ist.hour * 60 + dt_ist.minute
    return 9 * 60 + 15 <= mins <= 15 * 60 + 30


# ── Options data fetch ────────────────────────────────────────────

_INDEX_TO_NSE_SYMBOL = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX",
}


async def fetch_options_data_for_segment(
    base_symbol: str,
    segment: str,
    close_price: float,
) -> Dict[str, Any]:
    """
    Fetch and analyze options data for INDICES and INDIAN_FNO segments.
    Returns empty dict for all other segments (no options data needed).
    """
    if segment not in ("INDICES", "INDIAN_FNO"):
        return {}

    is_index = (segment == "INDICES")

    # Map symbol to NSE expected format
    nse_symbol = _INDEX_TO_NSE_SYMBOL.get(base_symbol.upper(), base_symbol.upper())

    logger.info(f"Fetching options chain: {nse_symbol} (index={is_index})")

    try:
        raw_data = await fetch_nse_option_chain(nse_symbol, is_index=is_index)
        if not raw_data:
            logger.warning(f"No options data returned for {nse_symbol}")
            return {}

        # Calculate days to nearest expiry (approximate — weekly = 7, monthly = 30)
        now_ist = datetime.now(IST)
        # Find nearest Thursday for weekly expiry
        days_to_thursday = (3 - now_ist.weekday()) % 7
        if days_to_thursday == 0:
            days_to_thursday = 7
        days_to_expiry = max(1, days_to_thursday)

        analysis = analyze_option_chain(raw_data, close_price, days_to_expiry)
        return analysis

    except Exception as e:
        logger.error(f"Options data fetch failed for {nse_symbol}: {e}")
        return {}


# ── Setup type derivation ─────────────────────────────────────────

def derive_setup_description(payload: NormalizedPayload) -> str:
    """Build human-readable setup description from payload flags."""
    parts = []

    direction = "Bullish" if payload.direction == "LONG" else "Bearish"

    if payload.signal_type == "OB_ENTRY":
        if payload.fvg_present:
            parts.append(f"{direction} OB + FVG overlap")
        else:
            parts.append(f"{direction} OB entry")
    elif payload.signal_type == "CHOCH":
        parts.append(f"CHOCH — {direction} structure reversal")
    elif payload.signal_type == "BOS":
        parts.append(f"BOS — {direction} continuation")
    elif payload.signal_type == "SWEEP":
        parts.append(f"Liquidity sweep + {direction} reaction")
    elif payload.signal_type == "TRAP":
        parts.append(f"{'Bull' if payload.direction == 'SHORT' else 'Bear'} Trap confirmed")
    else:
        parts.append(f"{direction} {payload.signal_type}")

    if payload.structure:
        parts.append(f"| {payload.structure} confirmed")

    return " ".join(parts)


def derive_zone_type(payload: NormalizedPayload) -> str:
    """Determine premium/discount zone label."""
    if payload.in_discount is True:
        return "Discount (below equilibrium)"
    elif payload.in_discount is False:
        return "Premium (above equilibrium)"
    else:
        return "Equilibrium zone"


def derive_narrative(payload: NormalizedPayload, options_data: Dict) -> str:
    """Generate 2-3 line plain English setup narrative."""
    direction = "long" if payload.direction == "LONG" else "short"
    zone = "discount" if payload.in_discount else "premium" if payload.in_discount is False else "equilibrium"
    instrument = payload.base_symbol or payload.instrument

    line1 = (
        f"Price entering {zone} zone at the {derive_setup_description(payload)}. "
    )
    line2 = (
        f"HTF structure is {payload.htf_bias.lower()}. "
        f"{'Liquidity has been swept — reduced stop hunt risk. ' if payload.liquidity_swept else ''}"
        f"{'FVG present as additional target/entry confluence. ' if payload.fvg_present else ''}"
    )

    opts_narrative = ""
    if options_data.get("pcr"):
        pcr = options_data["pcr"]
        opts_dir = options_data.get("options_direction", "NEUTRAL")
        if opts_dir != "NEUTRAL":
            opts_narrative = (
                f"Options data {opts_dir.lower()} (PCR {pcr:.2f}). "
            )

    return (line1 + line2 + opts_narrative).strip()


# ── Main processor ────────────────────────────────────────────────

async def process_smc_payload(
    payload: NormalizedPayload,
) -> Dict[str, Any]:
    """
    Enrich the normalized webhook payload with all derived context.
    Returns a fully enriched dict ready for the scorer.

    Does NOT modify the database — pure data transformation.
    """
    now_ist = datetime.now(IST)

    # ── Time context ───────────────────────────────────────────────
    active_session = detect_active_session(now_ist) or payload.session
    in_killzone    = detect_killzone(now_ist) or payload.is_killzone
    in_lunch       = is_lunch_hour(now_ist)
    market_open    = is_indian_market_open(now_ist)

    # ── Options data ───────────────────────────────────────────────
    options_data = await fetch_options_data_for_segment(
        base_symbol=payload.base_symbol,
        segment=payload.segment or "",
        close_price=payload.close,
    )

    # ── Confluence flags ───────────────────────────────────────────
    htf_matches_direction = (
        (payload.direction == "LONG" and payload.htf_bias == "BULLISH") or
        (payload.direction == "SHORT" and payload.htf_bias == "BEARISH")
    )

    options_confirm = False
    if options_data:
        opts_dir = options_data.get("options_direction", "NEUTRAL")
        options_confirm = (
            (payload.direction == "LONG" and opts_dir == "BULLISH") or
            (payload.direction == "SHORT" and opts_dir == "BEARISH")
        )

    near_max_pain = False
    if options_data.get("max_pain") and payload.close:
        near_max_pain = is_near_max_pain(payload.close, options_data["max_pain"])

    # ── Setup context ──────────────────────────────────────────────
    setup_type   = derive_setup_description(payload)
    zone_type    = derive_zone_type(payload)
    narrative    = derive_narrative(payload, options_data)

    # LTF CHOCH confirmed flag (inferred from signal_type OR structure field)
    ltf_choch = (
        payload.signal_type == "CHOCH" or
        payload.structure in ("CHOCH", "LTF_CHOCH")
    )

    # Build enriched dict
    enriched: Dict[str, Any] = {
        # Core signal fields
        "instrument":       payload.instrument,
        "base_symbol":      payload.base_symbol,
        "exchange":         payload.exchange,
        "segment":          payload.segment,
        "direction":        payload.direction,
        "timeframe":        payload.timeframe,
        "signal_type":      payload.signal_type,
        "structure":        payload.structure,

        # OB / FVG data
        "ob_high":          payload.ob_high,
        "ob_low":           payload.ob_low,
        "ob_mid":           payload.ob_mid,
        "fvg_present":      payload.fvg_present,
        "fvg_high":         payload.fvg_high,
        "fvg_low":          payload.fvg_low,
        "close":            payload.close,

        # Time and session
        "session":          active_session,
        "is_killzone":      in_killzone,
        "in_lunch":         in_lunch,
        "market_open":      market_open,
        "received_at":      payload.received_at,

        # Liquidity and structure
        "liquidity_swept":  payload.liquidity_swept,
        "htf_bias":         payload.htf_bias,
        "in_discount":      payload.in_discount,
        "trap_present":     payload.trap_present,
        "trap_direction":   payload.trap_direction,
        "volume_ratio":     payload.volume_ratio,
        "ltf_choch":        ltf_choch,

        # Derived confluence flags
        "htf_matches_direction": htf_matches_direction,
        "options_confirm":       options_confirm,
        "near_max_pain":         near_max_pain,

        # Options data (INDICES / INDIAN_FNO only)
        "options_pcr":           options_data.get("pcr"),
        "options_oi_bias":       options_data.get("options_direction"),
        "max_pain":              options_data.get("max_pain"),
        "gex":                   options_data.get("gex"),
        "ce_walls":              options_data.get("ce_walls", []),
        "pe_walls":              options_data.get("pe_walls", []),
        "iv_skew":               options_data.get("iv_skew"),

        # Descriptions
        "setup_type":            setup_type,
        "zone_type":             zone_type,
        "narrative":             narrative,
        "htf_timeframe":         "4H",  # TradingView sends LTF; HTF context is 4H default

        # Confluences dict (used in Telegram message)
        "confluences": {
            "htf_bias":        payload.htf_bias,
            "poi_type":        setup_type,
            "zone_type":       zone_type,
            "liquidity_swept": payload.liquidity_swept,
            "killzone":        in_killzone,
            "ltf_choch":       ltf_choch,
        },
    }

    logger.info(
        f"SMC payload enriched: {payload.instrument} {payload.direction} "
        f"session={active_session} killzone={in_killzone} "
        f"options_confirm={options_confirm} near_max_pain={near_max_pain}"
    )
    return enriched
