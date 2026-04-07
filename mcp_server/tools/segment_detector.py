"""
tools/segment_detector.py — Detects market segment from ticker + exchange string.
Maps incoming TradingView webhook data to one of 7 defined segments.
"""

import logging
import re
from typing import Optional

from mcp_server.config import FNO_SYMBOLS, INDEX_SYMBOLS, MCX_COMMODITIES

logger = logging.getLogger(__name__)

# ── Crypto exchange prefixes ───────────────────────────────────────
CRYPTO_EXCHANGES = {
    "BINANCE", "COINBASE", "KRAKEN", "BYBIT", "OKX",
    "HUOBI", "KUCOIN", "BITMEX", "DERIBIT",
}

# ── Global index exchanges / tickers ──────────────────────────────
GLOBAL_INDEX_EXCHANGES = {"INDEX", "OANDA", "SPREADEX", "FOREXCOM", "FX_IDC"}
GLOBAL_INDEX_TICKERS = {
    "SPX", "SPX500", "US500", "NAS100", "NDX", "NASDAQ100",
    "US30", "DJI", "DAX", "FDAX", "FTSE", "FTSE100",
    "NI225", "JP225", "HSI", "HK50", "ASX200", "AU200",
    "CAC40", "IBEX35", "IT40",
}

# ── Currency forex tickers ─────────────────────────────────────────
FOREX_PAIRS = {
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD",
    "USDCHF", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY",
}

# ── NSE currency pairs ────────────────────────────────────────────
NSE_CURRENCY_SUFFIXES = {"INR", "INRFUT"}
NSE_CURRENCY_PAIRS = {
    "USDINR", "EURINR", "GBPINR", "JPYINR",
    "USDINRFUT", "EURINRFUT",
}


def detect_segment(instrument: str, exchange: str) -> Optional[str]:
    """
    Determine which market segment a ticker belongs to.

    Returns one of:
      INDICES, INDIAN_FNO, INDIAN_STOCKS, COMMODITY,
      CURRENCY_FOREX, CRYPTO, GLOBAL_INDICES
    or None if unrecognized.

    Args:
        instrument: Raw ticker string from TradingView (e.g. "NSE:NIFTY" or just "NIFTY")
        exchange:   Exchange string from TradingView (e.g. "NSE", "MCX", "BINANCE")
    """
    # Normalize
    instrument = (instrument or "").upper().strip()
    exchange   = (exchange or "").upper().strip()

    # Strip exchange prefix if present (e.g. "NSE:NIFTY" → "NIFTY")
    if ":" in instrument:
        exchange_prefix, ticker = instrument.split(":", 1)
        if not exchange:
            exchange = exchange_prefix
        instrument = ticker
    else:
        ticker = instrument

    logger.debug(f"Segment detection: ticker={ticker} exchange={exchange}")

    # ── Crypto ────────────────────────────────────────────────────
    if exchange in CRYPTO_EXCHANGES:
        return "CRYPTO"

    # ── MCX Commodity ─────────────────────────────────────────────
    if exchange == "MCX":
        return "COMMODITY"

    # ── Global Indices ────────────────────────────────────────────
    if exchange in GLOBAL_INDEX_EXCHANGES:
        return "GLOBAL_INDICES"

    # Check ticker against known global index names
    base_ticker = re.sub(r"\d+$", "", ticker)  # strip trailing digits (DAX40 → DAX)
    if base_ticker in GLOBAL_INDEX_TICKERS or ticker in GLOBAL_INDEX_TICKERS:
        return "GLOBAL_INDICES"

    # ── NSE / BSE logic ───────────────────────────────────────────
    if exchange in ("NSE", "BSE") or not exchange:

        # Currency pairs first
        if ticker in NSE_CURRENCY_PAIRS or any(ticker.endswith(s) for s in NSE_CURRENCY_SUFFIXES):
            return "CURRENCY_FOREX"

        # Index symbols
        clean = re.sub(r"(FUT|OPT|CE|PE|\d{2}[A-Z]{3}\d{2}).*$", "", ticker)
        if any(clean.startswith(idx) or clean == idx for idx in INDEX_SYMBOLS):
            return "INDICES"

        # BSE:SENSEX
        if "SENSEX" in ticker:
            return "INDICES"

        # FINNIFTY, MIDCPNIFTY
        if "NIFTY" in ticker or "BANKNIFTY" in ticker:
            return "INDICES"

        # F&O eligible stocks
        base_symbol = re.sub(r"(FUT|CE|PE|\d{2}[A-Z]{3}\d{2}|\d{5}).*$", "", ticker)
        if base_symbol in FNO_SYMBOLS:
            return "INDIAN_FNO"

        # Default NSE: Indian stocks
        return "INDIAN_STOCKS"

    # ── Forex pairs ───────────────────────────────────────────────
    if exchange in ("FX", "FXCM", "OANDA", "FX_IDC") or ticker in FOREX_PAIRS:
        return "CURRENCY_FOREX"

    # ── Unrecognized ──────────────────────────────────────────────
    logger.warning(f"Unrecognized segment for ticker={ticker} exchange={exchange}")
    return None


def get_base_symbol(instrument: str, exchange: str) -> str:
    """
    Extract the clean base symbol (strip futures/options suffixes).
    E.g. "NIFTY23AUGFUT" → "NIFTY", "RELIANCE23SEP1600CE" → "RELIANCE"
    """
    instrument = (instrument or "").upper().strip()
    if ":" in instrument:
        _, instrument = instrument.split(":", 1)

    # Strip common futures/options suffixes
    clean = re.sub(
        r"(FUT|CE|PE|\d{2}[A-Z]{3}\d{2,4}|\d{4,6}(CE|PE)?|-\d+).*$",
        "", instrument
    )
    return clean.strip() or instrument


def is_index_option(ticker: str, exchange: str) -> bool:
    """Check if ticker is an index option (for special options chain logic)."""
    segment = detect_segment(ticker, exchange)
    base = get_base_symbol(ticker, exchange)
    is_index = segment == "INDICES"
    is_option = ticker.upper().endswith(("CE", "PE"))
    return is_index and is_option
