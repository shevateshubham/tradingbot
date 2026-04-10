"""
tools/webhook_handler.py — TradingView webhook receiver.
Validates the secret, normalizes the payload, and dispatches
to the processing pipeline. All validation errors are logged.
"""

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator, ValidationInfo

from mcp_server.config import get_settings
from mcp_server.tools.segment_detector import detect_segment, get_base_symbol

logger = logging.getLogger(__name__)


# ── Webhook payload schema ─────────────────────────────────────────

class WebhookPayload(BaseModel):
    """
    Schema for TradingView webhook JSON.
    All fields sent from Pine Script alert message template.
    """
    secret:          str
    instrument:      str
    exchange:        str             = ""
    timeframe:       str             = "15"
    signal_type:     str             = "OB_ENTRY"   # OB_ENTRY, BOS, CHOCH, SWEEP, TRAP
    direction:       str             = "LONG"        # LONG or SHORT
    structure:       str             = ""            # CHOCH, BOS, CONTINUATION
    ob_high:         float           = 0.0
    ob_low:          float           = 0.0
    ob_mid:          float           = 0.0
    fvg_present:     bool            = False
    fvg_high:        Optional[float] = None
    fvg_low:         Optional[float] = None
    liquidity_swept: bool            = False
    session:         str             = "INDIA"       # ASIA, LONDON, NY, INDIA
    htf_bias:        str             = "BULLISH"     # BULLISH, BEARISH, NEUTRAL
    in_discount:     Optional[bool]  = None          # True = below 50% equilibrium
    trap_present:    bool            = False
    trap_direction:  str             = ""
    volume_ratio:    float           = 1.0
    close:           float           = 0.0
    high:            Optional[float] = None
    low:             Optional[float] = None
    spread:          Optional[float] = None           # bid-ask spread from Pine Script
    timestamp:       str             = ""

    @field_validator("direction")
    @classmethod
    def normalize_direction(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("signal_type")
    @classmethod
    def normalize_signal_type(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("htf_bias")
    @classmethod
    def normalize_htf_bias(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("ob_mid", mode="before")
    @classmethod
    def calculate_ob_mid(cls, v: float, info: ValidationInfo) -> float:
        """Auto-calculate OB midpoint if not provided."""
        if (v == 0.0 or v is None) and info.data:
            ob_high = info.data.get("ob_high", 0) or 0
            ob_low  = info.data.get("ob_low", 0)  or 0
            if ob_high and ob_low:
                return (ob_high + ob_low) / 2
        return v or 0.0

    model_config = {"extra": "allow"}  # Allow extra fields from TradingView without error


class NormalizedPayload(BaseModel):
    """
    Internal normalized payload after validation, enrichment and segment detection.
    This is what flows through the rest of the pipeline.
    """
    # Original fields
    instrument:      str
    exchange:        str
    base_symbol:     str             # e.g. "NIFTY" (stripped of FUT/options suffixes)
    timeframe:       str
    signal_type:     str
    direction:       str
    structure:       str
    ob_high:         float
    ob_low:          float
    ob_mid:          float
    fvg_present:     bool
    fvg_high:        Optional[float]
    fvg_low:         Optional[float]
    liquidity_swept: bool
    session:         str
    htf_bias:        str
    in_discount:     Optional[bool]
    trap_present:    bool
    trap_direction:  str
    volume_ratio:    float
    close:           float
    timestamp:       str

    # Enriched fields
    segment:         Optional[str]   # INDICES, INDIAN_FNO, etc.
    received_at:     datetime
    is_killzone:     bool            = False

    class Config:
        extra = "allow"


# ── Secret validation ─────────────────────────────────────────────

def validate_webhook_secret(incoming_secret: str) -> bool:
    """
    Compare incoming secret against TV_WEBHOOK_SECRET using constant-time comparison.
    Prevents timing attacks.
    """
    settings = get_settings()
    expected = settings.tv_webhook_secret.encode("utf-8")
    incoming = incoming_secret.encode("utf-8")
    return hmac.compare_digest(expected, incoming)


# ── Killzone detection ────────────────────────────────────────────

def _is_in_killzone(session: str, received_at: datetime) -> bool:
    """
    Check if signal time is within ±30 min of any session open.
    Pure time math — no indicators.
    """
    import pytz
    from mcp_server.config import SESSIONS, KILLZONE_MINUTES

    ist = pytz.timezone("Asia/Kolkata")
    try:
        if received_at.tzinfo is None:
            received_ist = ist.localize(received_at)
        else:
            received_ist = received_at.astimezone(ist)
    except Exception:
        return False

    current_minutes = received_ist.hour * 60 + received_ist.minute

    for sess_name, sess_times in SESSIONS.items():
        try:
            open_h, open_m = map(int, sess_times["start"].split(":"))
            open_minutes = open_h * 60 + open_m
            diff = abs(current_minutes - open_minutes)
            if diff <= KILLZONE_MINUTES or diff >= (1440 - KILLZONE_MINUTES):
                return True
        except Exception:
            continue
    return False


# ── Main handler ──────────────────────────────────────────────────

async def handle_webhook(raw_body: dict) -> Tuple[Optional[NormalizedPayload], str]:
    """
    Validate and normalize an incoming TradingView webhook payload.

    Returns:
        (NormalizedPayload, "ok") on success
        (None, "error reason") on failure
    """
    # ── Parse payload ─────────────────────────────────────────────
    try:
        payload = WebhookPayload(**raw_body)
    except Exception as e:
        logger.warning(f"Webhook payload validation failed: {e}")
        return None, f"invalid_payload: {e}"

    # ── Validate secret ───────────────────────────────────────────
    if not validate_webhook_secret(payload.secret):
        logger.warning(
            f"Invalid webhook secret from instrument={payload.instrument}. "
            "Check TV_WEBHOOK_SECRET matches TradingView alert JSON."
        )
        return None, "invalid_secret"

    # ── Detect segment ────────────────────────────────────────────
    segment = detect_segment(payload.instrument, payload.exchange)
    if segment is None:
        logger.warning(
            f"Cannot detect segment for instrument={payload.instrument} "
            f"exchange={payload.exchange}"
        )
        # Don't reject — log and let filter handle it
        segment = "UNKNOWN"

    base_symbol = get_base_symbol(payload.instrument, payload.exchange)

    # ── Build normalized payload ──────────────────────────────────
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    is_killzone = _is_in_killzone(payload.session, now)

    normalized = NormalizedPayload(
        instrument=payload.instrument,
        exchange=payload.exchange,
        base_symbol=base_symbol,
        timeframe=payload.timeframe,
        signal_type=payload.signal_type,
        direction=payload.direction,
        structure=payload.structure,
        ob_high=payload.ob_high,
        ob_low=payload.ob_low,
        ob_mid=payload.ob_mid,
        fvg_present=payload.fvg_present,
        fvg_high=payload.fvg_high,
        fvg_low=payload.fvg_low,
        liquidity_swept=payload.liquidity_swept,
        session=payload.session,
        htf_bias=payload.htf_bias,
        in_discount=payload.in_discount,
        trap_present=payload.trap_present,
        trap_direction=payload.trap_direction,
        volume_ratio=payload.volume_ratio,
        close=payload.close,
        timestamp=payload.timestamp,
        segment=segment,
        received_at=now,
        is_killzone=is_killzone,
    )

    logger.info(
        f"Webhook accepted: {normalized.instrument} {normalized.direction} "
        f"tf={normalized.timeframe} segment={segment} "
        f"killzone={is_killzone}"
    )
    return normalized, "ok"
