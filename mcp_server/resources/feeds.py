"""
resources/feeds.py — External data feeds.
NSE option chain API (cookie rotation) and Binance public endpoints.
No indicators. Raw data only.
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any

import httpx

from mcp_server.config import (
    NSE_BASE_URL, NSE_OPTION_CHAIN_INDEX, NSE_OPTION_CHAIN_EQUITY,
    NSE_COOKIE_REFRESH_MINUTES, BINANCE_BASE_URL, BINANCE_FUTURES_URL,
)

logger = logging.getLogger(__name__)

# ── NSE cookie state ──────────────────────────────────────────────

_nse_cookies: Dict[str, str] = {}
_nse_cookie_fetched_at: float = 0.0
_nse_lock = asyncio.Lock()

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}


async def _refresh_nse_cookies() -> Dict[str, str]:
    """
    Hit the NSE homepage to get fresh session cookies.
    NSE rejects API calls without these cookies.
    Rotated every NSE_COOKIE_REFRESH_MINUTES minutes.
    """
    global _nse_cookies, _nse_cookie_fetched_at

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers=NSE_HEADERS,
        ) as client:
            # Hit homepage first to get cookies
            resp = await client.get(NSE_BASE_URL)
            resp.raise_for_status()
            cookies = dict(resp.cookies)

            # Also hit option-chain page for any additional cookies
            await asyncio.sleep(0.5)
            await client.get(f"{NSE_BASE_URL}/option-chain")

            cookies.update(dict(resp.cookies))
            _nse_cookies = cookies
            _nse_cookie_fetched_at = time.time()
            logger.info(f"NSE cookies refreshed: {list(cookies.keys())}")
            return cookies

    except Exception as e:
        logger.error(f"NSE cookie refresh failed: {e}")
        return _nse_cookies  # Return stale if refresh fails


async def _get_nse_cookies() -> Dict[str, str]:
    """Return valid NSE cookies, refreshing if expired."""
    async with _nse_lock:
        age_seconds = time.time() - _nse_cookie_fetched_at
        if not _nse_cookies or age_seconds > (NSE_COOKIE_REFRESH_MINUTES * 60):
            return await _refresh_nse_cookies()
        return _nse_cookies


# ── NSE Option Chain ──────────────────────────────────────────────

async def fetch_nse_option_chain(symbol: str, is_index: bool = True) -> Optional[Dict[str, Any]]:
    """
    Fetch raw NSE option chain data for a symbol.
    Returns the full JSON response or None on failure.

    Args:
        symbol:   Symbol name (e.g. "NIFTY", "BANKNIFTY", "RELIANCE")
        is_index: True for index options, False for equity options
    """
    cookies = await _get_nse_cookies()
    endpoint = NSE_OPTION_CHAIN_INDEX if is_index else NSE_OPTION_CHAIN_EQUITY
    url = f"{NSE_BASE_URL}{endpoint}?symbol={symbol.upper()}"

    for attempt in range(3):
        try:
            await asyncio.sleep(attempt * 1.0)  # backoff on retry

            async with httpx.AsyncClient(
                timeout=20.0,
                cookies=cookies,
                headers={**NSE_HEADERS, "Host": "www.nseindia.com"},
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)

                # NSE returns 401 when cookies are stale
                if resp.status_code == 401:
                    logger.info("NSE cookies stale, refreshing...")
                    cookies = await _refresh_nse_cookies()
                    continue

                resp.raise_for_status()
                data = resp.json()

                if "records" not in data:
                    logger.warning(f"NSE response missing 'records' key for {symbol}")
                    return None

                logger.info(f"NSE option chain fetched: {symbol} ({len(data.get('records', {}).get('data', []))} strikes)")
                return data

        except httpx.HTTPStatusError as e:
            logger.warning(f"NSE HTTP error (attempt {attempt+1}): {e.response.status_code}")
            if attempt == 2:
                return None
        except Exception as e:
            logger.error(f"NSE fetch error for {symbol} (attempt {attempt+1}): {e}")
            if attempt == 2:
                return None

    return None


# ── Binance Public Endpoints ──────────────────────────────────────

async def fetch_binance_klines(
    symbol: str,
    interval: str = "15m",
    limit: int = 200,
    futures: bool = False,
) -> Optional[list]:
    """
    Fetch raw OHLCV klines from Binance public API.
    No API key required.

    Returns list of [open_time, open, high, low, close, volume, ...] or None.
    """
    base = BINANCE_FUTURES_URL if futures else BINANCE_BASE_URL
    endpoint = "/fapi/v1/klines" if futures else "/api/v3/klines"
    url = f"{base}{endpoint}"

    params = {
        "symbol": symbol.upper().replace("/", ""),
        "interval": interval,
        "limit": limit,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            logger.debug(f"Binance klines: {symbol} {interval} — {len(data)} candles")
            return data
    except Exception as e:
        logger.error(f"Binance klines fetch failed for {symbol}: {e}")
        return None


async def fetch_binance_ticker_24h(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Fetch 24-hour ticker stats from Binance public API.
    Returns raw JSON with lastPrice, volume, priceChangePercent, etc.
    """
    url = f"{BINANCE_BASE_URL}/api/v3/ticker/24hr"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"symbol": symbol.upper().replace("/", "")})
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Binance 24h ticker failed for {symbol}: {e}")
        return None


async def fetch_binance_depth(symbol: str, limit: int = 20) -> Optional[Dict[str, Any]]:
    """
    Fetch raw order book depth from Binance.
    Returns {bids: [[price, qty], ...], asks: [[price, qty], ...]} or None.
    """
    url = f"{BINANCE_BASE_URL}/api/v3/depth"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"symbol": symbol.upper().replace("/", ""), "limit": limit})
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Binance depth fetch failed for {symbol}: {e}")
        return None


# ── Current price fetcher (for price tracking) ─────────────────────

async def fetch_current_price(instrument: str, segment: str) -> Optional[float]:
    """
    Fetch the current market price for a given instrument.
    Used by the price tracker to check SL/TP levels every 5 minutes.
    """
    try:
        if segment == "CRYPTO":
            # Extract base symbol (BTCUSDT → BTCUSDT)
            symbol = instrument.replace("BINANCE:", "").replace("COINBASE:", "")
            if "/" not in symbol and "USDT" not in symbol:
                symbol = symbol + "USDT"
            ticker = await fetch_binance_ticker_24h(symbol)
            if ticker:
                return float(ticker.get("lastPrice", 0))

        elif segment in ("INDICES", "INDIAN_FNO", "INDIAN_STOCKS"):
            # Use NSE API for current price
            return await _fetch_nse_price(instrument)

        elif segment == "COMMODITY":
            # MCX — could use NSE API or a commodity data source
            return await _fetch_mcx_price(instrument)

    except Exception as e:
        logger.error(f"Price fetch failed for {instrument}: {e}")

    return None


async def _fetch_nse_price(instrument: str) -> Optional[float]:
    """Fetch current price from NSE quotes API."""
    clean = instrument.replace("NSE:", "").replace("BSE:", "")
    url = f"{NSE_BASE_URL}/api/quote-equity?symbol={clean}"
    cookies = await _get_nse_cookies()

    try:
        async with httpx.AsyncClient(
            timeout=10.0, cookies=cookies, headers=NSE_HEADERS
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                # NSE returns priceInfo.lastPrice
                price_info = data.get("priceInfo", {})
                return float(price_info.get("lastPrice", 0)) or None
    except Exception as e:
        logger.debug(f"NSE price fetch failed for {instrument}: {e}")
    return None


async def _fetch_mcx_price(instrument: str) -> Optional[float]:
    """Fetch MCX commodity price — placeholder for MCX public API."""
    # MCX doesn't have a simple public REST API like NSE
    # In production, integrate with an MCX data provider
    logger.debug(f"MCX price fetch not implemented for {instrument}")
    return None
