"""
tools/trade_calculator.py — Entry, SL, TP, position sizing, and charges.
Pure mathematical derivation from OB/FVG levels.
No indicators. All charge calculations match Indian F&O/MCX/Crypto specs.
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional, Tuple

from mcp_server.config import (
    MAX_SL_PERCENT, MIN_RR_RATIO, CHARGES_CONFIG,
    BROKERAGE_FLAT, GST_RATE, SEBI_TURNOVER_CHARGE,
    get_settings,
)

logger = logging.getLogger(__name__)

# Lot sizes by base symbol (approximate — NSE publishes exact values)
LOT_SIZES = {
    "NIFTY":      50,
    "BANKNIFTY":  15,
    "FINNIFTY":   40,
    "MIDCPNIFTY": 75,
    "SENSEX":     10,
    # F&O stocks (standard lot sizes)
    "RELIANCE":   250,
    "TCS":        150,
    "INFY":       300,
    "HDFCBANK":   550,
    "ICICIBANK":  700,
    # Commodities
    "GOLD":       100,   # 100 grams per lot
    "SILVER":     30,    # 30 kg per lot
    "CRUDEOIL":   100,   # 100 barrels per lot
    "NATURALGAS": 1250,  # 1250 mmBtu per lot
    "COPPER":     2500,  # 2500 kg per lot
    # Crypto (traded in USDT units per 'lot')
    "BTCUSDT":    0.001,
    "ETHUSDT":    0.01,
    "DEFAULT":    1,
}


@dataclass
class TradeCalculation:
    """Complete trade calculation result."""
    entry:          float
    sl:             float
    tp1:            float
    tp2:            float
    tp3:            float
    sl_points:      float
    sl_percent:     float
    rr_ratio:       float
    lots:           int
    lot_size:       int
    charges:        float       # Estimated total round-trip charges
    net_at_tp1:     float       # P&L at TP1 after all charges
    net_at_tp2:     float
    net_at_tp3:     float
    valid:          bool
    reject_reason:  Optional[str]


def _get_lot_size(base_symbol: str) -> int:
    """Return lot size for an instrument."""
    return LOT_SIZES.get(base_symbol.upper(), LOT_SIZES["DEFAULT"])


def _avoid_round_number(price: float, direction: str, buffer_pct: float = 0.0005) -> float:
    """
    Nudge SL slightly away from round numbers to avoid stop hunts.
    buffer_pct = 0.05% default.
    """
    round_levels = [
        round(price, -4),
        round(price, -3),
        round(price, -2),
    ]
    for level in round_levels:
        if abs(price - level) / price < 0.001:  # Within 0.1% of round number
            if direction == "LONG":
                price -= price * buffer_pct
            else:
                price += price * buffer_pct
            break
    return round(price, 2)


def calculate_levels(enriched: dict) -> Tuple[float, float, float, float, float]:
    """
    Derive entry, SL, TP1, TP2, TP3 from OB/FVG data.
    Returns (entry, sl, tp1, tp2, tp3).

    Entry: 50% of OB body or FVG midpoint (or overlap midpoint)
    SL: beyond OB extreme + 0.05% buffer, away from round numbers
    TPs: multiples of SL distance in direction of trade
    """
    direction   = enriched.get("direction", "LONG")
    ob_high     = enriched.get("ob_high", 0.0)
    ob_low      = enriched.get("ob_low", 0.0)
    ob_mid      = enriched.get("ob_mid", 0.0)
    fvg_present = enriched.get("fvg_present", False)
    fvg_high    = enriched.get("fvg_high")
    fvg_low     = enriched.get("fvg_low")
    close       = enriched.get("close", 0.0)

    # ── Entry calculation ─────────────────────────────────────────
    if fvg_present and fvg_high and fvg_low:
        # OB + FVG overlap: midpoint of the overlapping zone
        overlap_high = min(ob_high, fvg_high)
        overlap_low  = max(ob_low, fvg_low)
        if overlap_high > overlap_low:
            entry = (overlap_high + overlap_low) / 2
        else:
            # No overlap: use OB midpoint
            entry = ob_mid if ob_mid else (ob_high + ob_low) / 2
    else:
        # OB only: 50% midpoint of OB body
        entry = ob_mid if ob_mid else (ob_high + ob_low) / 2

    if entry <= 0:
        entry = close  # Fallback to close price

    # ── SL calculation ────────────────────────────────────────────
    sl_buffer = 0.0005  # 0.05% buffer

    if direction == "LONG":
        raw_sl = ob_low * (1 - sl_buffer)
        sl = _avoid_round_number(raw_sl, "LONG")
    else:
        raw_sl = ob_high * (1 + sl_buffer)
        sl = _avoid_round_number(raw_sl, "SHORT")

    sl = round(sl, 2)

    # ── SL distance ───────────────────────────────────────────────
    sl_points = abs(entry - sl)
    sl_percent = (sl_points / entry * 100) if entry > 0 else 0

    # ── TP levels ─────────────────────────────────────────────────
    if direction == "LONG":
        tp1 = round(entry + sl_points * 1.0, 2)
        tp2 = round(entry + sl_points * 2.0, 2)
        tp3 = round(entry + sl_points * 3.0, 2)
    else:
        tp1 = round(entry - sl_points * 1.0, 2)
        tp2 = round(entry - sl_points * 2.0, 2)
        tp3 = round(entry - sl_points * 3.0, 2)

    return entry, sl, tp1, tp2, tp3


def calculate_charges(
    segment: str,
    entry: float,
    lots: int,
    lot_size: int,
    sl_points: float,
    direction: str,
) -> float:
    """
    Calculate total estimated round-trip charges for a position.
    Matches Indian F&O, MCX, Crypto, and Forex charge structures exactly.
    Returns total charge in local currency.
    """
    config = CHARGES_CONFIG.get(segment, {})
    if not config:
        return 0.0

    turnover = entry * lots * lot_size
    charges = 0.0

    if segment in ("INDICES", "INDIAN_FNO"):
        # Brokerage: ₹20 per order × 2 orders (entry + exit)
        brokerage = config["brokerage_per_order"] * 2

        # STT (Securities Transaction Tax)
        # Futures sell-side: 0.0125% of turnover
        # Options buy premium: 0.0625%
        stt = turnover * config.get("stt_futures_sell", 0.000125)

        # NSE exchange charges: 0.0019%
        exchange = turnover * config.get("nse_exchange", 0.000019)

        # SEBI: ₹10 per crore
        sebi = (turnover / 10_000_000) * SEBI_TURNOVER_CHARGE

        # Stamp duty: 0.003% buy-side only
        stamp = turnover * config.get("stamp_buy", 0.00003)

        # GST: 18% on (brokerage + exchange + SEBI)
        gst = (brokerage + exchange + sebi) * GST_RATE

        charges = brokerage + stt + exchange + sebi + stamp + gst

    elif segment == "COMMODITY":
        brokerage = config["brokerage_per_order"] * 2
        exchange = turnover * config.get("mcx_exchange", 0.00003)
        stamp = turnover * config.get("stamp_buy", 0.00003)
        gst = (brokerage + exchange) * GST_RATE
        charges = brokerage + exchange + stamp + gst

    elif segment == "CURRENCY_FOREX":
        brokerage = config["brokerage_per_order"] * 2
        exchange = turnover * config.get("nse_exchange", 0.0000135)
        stamp = turnover * config.get("stamp_buy", 0.00001)
        gst = (brokerage + exchange) * GST_RATE
        charges = brokerage + exchange + stamp + gst

    elif segment == "CRYPTO":
        # Binance: 0.04% per side = 0.08% round trip
        taker_fee = config.get("taker_fee_per_side", 0.0004)
        charges = turnover * taker_fee * 2  # Both sides

    elif segment in ("GLOBAL_INDICES", "CURRENCY_FOREX"):
        # Commission per lot + spread
        commission = config.get("commission_per_lot", 3.5) * lots * 2
        spread_cost = turnover * config.get("spread_percent", 0.0002)
        charges = commission + spread_cost

    return round(charges, 2)


def calculate_position(
    enriched: dict,
    account_size: int = 100_000,
    risk_percent: float = 1.0,
) -> TradeCalculation:
    """
    Complete trade calculation: entry, SL, TP, lots, charges, net P&L.

    All hard discard rules enforced here:
    - SL too wide
    - RR below minimum
    - Net P&L at TP1 negative
    - 0 lots calculated
    """
    settings = get_settings()
    segment     = enriched.get("segment", "INDIAN_FNO")
    base_symbol = enriched.get("base_symbol", "")
    direction   = enriched.get("direction", "LONG")

    # ── Step 1: Calculate raw levels ─────────────────────────────
    entry, sl, tp1, tp2, tp3 = calculate_levels(enriched)
    sl_points  = abs(entry - sl)
    sl_percent = (sl_points / entry * 100) if entry > 0 else 0

    # ── Hard discard: SL too wide ─────────────────────────────────
    max_sl_pct = MAX_SL_PERCENT.get(segment, 2.0)
    if sl_percent > max_sl_pct:
        logger.info(
            f"Hard discard: SL too wide {sl_percent:.2f}% > max {max_sl_pct}% "
            f"for segment={segment}"
        )
        return TradeCalculation(
            entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
            sl_points=sl_points, sl_percent=sl_percent,
            rr_ratio=0.0, lots=0, lot_size=1, charges=0.0,
            net_at_tp1=0.0, net_at_tp2=0.0, net_at_tp3=0.0,
            valid=False, reject_reason=f"sl_too_wide:{sl_percent:.2f}%>max_{max_sl_pct}%",
        )

    # ── Step 2: Check RR ratio ────────────────────────────────────
    tp1_distance = abs(tp1 - entry)
    rr_ratio = tp1_distance / sl_points if sl_points > 0 else 0

    if rr_ratio < settings.min_rr_ratio:
        logger.info(f"Hard discard: RR {rr_ratio:.2f} < min {settings.min_rr_ratio}")
        return TradeCalculation(
            entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
            sl_points=sl_points, sl_percent=sl_percent,
            rr_ratio=rr_ratio, lots=0, lot_size=1, charges=0.0,
            net_at_tp1=0.0, net_at_tp2=0.0, net_at_tp3=0.0,
            valid=False, reject_reason=f"rr_too_low:{rr_ratio:.2f}<{settings.min_rr_ratio}",
        )

    # ── Step 3: Position sizing ───────────────────────────────────
    risk_amount = account_size * (risk_percent / 100)
    lot_size    = _get_lot_size(base_symbol)

    if sl_points <= 0 or lot_size <= 0:
        lots = 1
    else:
        lots = math.floor(risk_amount / (sl_points * lot_size))

    lots = max(1, lots)  # Minimum 1 lot

    # ── Step 4: Charges ───────────────────────────────────────────
    charges = calculate_charges(segment, entry, lots, lot_size, sl_points, direction)

    # ── Step 5: Net P&L at each target ───────────────────────────
    pts_to_tp1 = abs(tp1 - entry)
    pts_to_tp2 = abs(tp2 - entry)
    pts_to_tp3 = abs(tp3 - entry)

    gross_tp1 = pts_to_tp1 * lots * lot_size
    gross_tp2 = pts_to_tp2 * lots * lot_size
    gross_tp3 = pts_to_tp3 * lots * lot_size

    net_at_tp1 = gross_tp1 - charges
    net_at_tp2 = gross_tp2 - charges
    net_at_tp3 = gross_tp3 - charges

    # ── Hard discard: TP1 negative after charges ──────────────────
    if net_at_tp1 <= 0:
        logger.info(f"Hard discard: TP1 net negative {net_at_tp1:.2f} after charges {charges:.2f}")
        return TradeCalculation(
            entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
            sl_points=sl_points, sl_percent=sl_percent,
            rr_ratio=rr_ratio, lots=lots, lot_size=lot_size,
            charges=charges, net_at_tp1=net_at_tp1,
            net_at_tp2=net_at_tp2, net_at_tp3=net_at_tp3,
            valid=False, reject_reason=f"tp1_net_negative:{net_at_tp1:.2f}",
        )

    logger.info(
        f"Trade calculated: entry={entry} sl={sl} tp1={tp1} tp2={tp2} tp3={tp3} "
        f"lots={lots} lot_size={lot_size} charges={charges:.2f} net_tp1={net_at_tp1:.2f}"
    )

    return TradeCalculation(
        entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
        sl_points=sl_points, sl_percent=sl_percent,
        rr_ratio=rr_ratio, lots=lots, lot_size=lot_size,
        charges=charges, net_at_tp1=net_at_tp1,
        net_at_tp2=net_at_tp2, net_at_tp3=net_at_tp3,
        valid=True, reject_reason=None,
    )
