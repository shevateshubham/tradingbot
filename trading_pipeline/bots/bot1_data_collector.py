"""
Bot 1: Data Collector Bot
Fetches OHLCV data ONLY for the selected market/segment.

Data sources:
  - NIFTY/FOREX/GOLD : Twelve Data API (free, 800 calls/day, no datacenter block)
                       Set TWELVE_DATA_API_KEY env var (free at twelvedata.com)
  - CRYPTO           : OKX via ccxt (no API key, globally accessible)

Yahoo Finance is NOT used — its US-based servers block Railway's datacenter IPs
with HTTP 429 regardless of headers/cookies.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from utils.config_loader import load_config
from utils.logger import get_logger
from utils.market_hours import is_market_open, get_closed_markets_message

logger = get_logger("bot1")

# ── Twelve Data ────────────────────────────────────────────────────────────────

TWELVE_DATA_BASE = "https://api.twelvedata.com/time_series"

# Map config symbols → Twelve Data symbols
TWELVE_DATA_SYMBOLS: dict[str, str] = {
    # NIFTY stocks
    "RELIANCE.NS":  "RELIANCE:NSE",
    "HDFCBANK.NS":  "HDFCBANK:NSE",
    "TCS.NS":       "TCS:NSE",
    "INFY.NS":      "INFY:NSE",
    "ICICIBANK.NS": "ICICIBANK:NSE",
    "WIPRO.NS":     "WIPRO:NSE",
    "SBIN.NS":      "SBIN:NSE",
    # Forex
    "EURUSD=X":     "EUR/USD",
    "GBPUSD=X":     "GBP/USD",
    "USDJPY=X":     "USD/JPY",
    "AUDUSD=X":     "AUD/USD",
    "USDCAD=X":     "USD/CAD",
    # Gold / Commodities
    "GC=F":         "XAU/USD",
    "SI=F":         "XAG/USD",
}

# Map config timeframe → Twelve Data interval
TWELVE_DATA_TF: dict[str, str] = {
    "1m":  "1min",
    "5m":  "5min",
    "15m": "15min",
    "1h":  "1h",
    "4h":  "4h",
    "1d":  "1day",
}

# ── OKX (CRYPTO) ───────────────────────────────────────────────────────────────

CCXT_SYMBOL_MAP: dict[str, str] = {
    "BTC-USD": "BTC/USDT",
    "ETH-USD": "ETH/USDT",
    "BNB-USD": "BNB/USDT",
    "SOL-USD": "SOL/USDT",
}


# ── Fetchers ───────────────────────────────────────────────────────────────────

def _fetch_twelve_data(symbol: str, timeframes: list[str], min_bars: int) -> dict:
    """
    Fetch OHLCV from Twelve Data API.
    Requires TWELVE_DATA_API_KEY environment variable (free at twelvedata.com).
    800 API credits/day on free tier — 1 credit per request.
    """
    import requests

    api_key = os.environ.get("TWELVE_DATA_API_KEY", "")
    if not api_key:
        return {"fetch_status": "error", "error": "TWELVE_DATA_API_KEY not set"}

    td_symbol = TWELVE_DATA_SYMBOLS.get(symbol)
    if not td_symbol:
        return {"fetch_status": "error", "error": f"No Twelve Data mapping for {symbol}"}

    tf_data: dict = {}
    session = requests.Session()
    session.headers["User-Agent"] = "TradingBot/1.0"

    for tf in timeframes:
        td_interval = TWELVE_DATA_TF.get(tf)
        if not td_interval:
            tf_data[tf] = None
            continue

        try:
            resp = session.get(
                TWELVE_DATA_BASE,
                params={
                    "symbol":     td_symbol,
                    "interval":   td_interval,
                    "apikey":     api_key,
                    "outputsize": 100,
                    "order":      "ASC",
                },
                timeout=15,
            )

            if resp.status_code != 200:
                logger.warning(f"{symbol} {tf}: Twelve Data HTTP {resp.status_code}")
                tf_data[tf] = None
                continue

            data = resp.json()

            if data.get("status") == "error":
                logger.warning(f"{symbol} {tf}: Twelve Data error — {data.get('message')}")
                tf_data[tf] = None
                continue

            values = data.get("values", [])
            if len(values) < min_bars:
                logger.warning(f"{symbol} {tf}: only {len(values)} bars (need {min_bars})")
                tf_data[tf] = None
                continue

            tf_data[tf] = {
                "opens":      [float(v["open"])   for v in values],
                "highs":      [float(v["high"])   for v in values],
                "lows":       [float(v["low"])    for v in values],
                "closes":     [float(v["close"])  for v in values],
                "volumes":    [float(v.get("volume", 0) or 0) for v in values],
                "timestamps": [v["datetime"]      for v in values],
                "count":      len(values),
            }
            time.sleep(0.2)   # Stay within Twelve Data rate limits

        except Exception as e:
            logger.error(f"{symbol} {tf} Twelve Data error: {e}")
            tf_data[tf] = None

    current_price = None
    try:
        closes = (tf_data.get("15m") or tf_data.get("5m") or tf_data.get("1m") or {}).get("closes")
        if closes:
            current_price = closes[-1]
    except Exception:
        pass

    return {
        "timeframes":    tf_data,
        "current_price": current_price,
        "fetch_status":  "ok" if any(v is not None for v in tf_data.values()) else "error",
        "fetched_at":    datetime.utcnow().isoformat(),
    }


def _fetch_ccxt(symbol: str, timeframes: list[str], min_bars: int) -> dict:
    """Fetch OHLCV from OKX public API via ccxt (no API key, no geo-restriction)."""
    try:
        import ccxt
    except ImportError:
        return {"fetch_status": "error", "error": "ccxt not installed"}

    ccxt_symbol = CCXT_SYMBOL_MAP.get(symbol, symbol.replace("-", "/").replace("USD", "USDT"))

    try:
        exchange = ccxt.okx({"enableRateLimit": True})
    except Exception as e:
        return {"fetch_status": "error", "error": str(e)}

    tf_data: dict = {}
    for tf in timeframes:
        try:
            ohlcv = exchange.fetch_ohlcv(ccxt_symbol, tf, limit=100)
            if not ohlcv or len(ohlcv) < min_bars:
                tf_data[tf] = None
                logger.warning(f"{symbol} {tf}: only {len(ohlcv)} bars from OKX")
                continue

            tf_data[tf] = {
                "opens":      [c[1] for c in ohlcv],
                "highs":      [c[2] for c in ohlcv],
                "lows":       [c[3] for c in ohlcv],
                "closes":     [c[4] for c in ohlcv],
                "volumes":    [c[5] for c in ohlcv],
                "timestamps": [str(datetime.utcfromtimestamp(c[0] / 1000)) for c in ohlcv],
                "count":      len(ohlcv),
            }
            time.sleep(0.3)
        except Exception as e:
            logger.error(f"{symbol} {tf} ccxt error: {e}")
            tf_data[tf] = None

    current_price = None
    try:
        ticker = exchange.fetch_ticker(ccxt_symbol)
        current_price = ticker.get("last")
    except Exception:
        closes = (tf_data.get("1m") or tf_data.get("5m") or {}).get("closes")
        if closes:
            current_price = closes[-1]

    return {
        "timeframes":    tf_data,
        "current_price": current_price,
        "fetch_status":  "ok" if any(v is not None for v in tf_data.values()) else "error",
        "fetched_at":    datetime.utcnow().isoformat(),
    }


def _fetch_symbol(symbol: str, market: str, timeframes: list[str], min_bars: int) -> dict:
    """Dispatch to correct fetcher based on market type."""
    if market == "CRYPTO":
        return _fetch_ccxt(symbol, timeframes, min_bars)
    return _fetch_twelve_data(symbol, timeframes, min_bars)


class DataCollectorBot:
    """
    Bot 1: Fetches OHLCV data for all symbols in the selected market.
    Only fetches data for the market requested — no wasted API calls.
    """

    def run(self, pipeline_dict: dict) -> dict:
        config     = load_config()
        market     = pipeline_dict.get("selected_market", "ALL")
        timeframes = config.get("timeframes", ["1m", "5m", "15m"])
        min_bars   = config.get("min_bars_required", 30)

        symbols_to_fetch: dict[str, str] = {}
        if market == "ALL":
            for mkt, syms in config.get("symbols", {}).items():
                disabled = set(config.get("disabled_symbols", []))
                for sym in syms:
                    if sym not in disabled:
                        symbols_to_fetch[sym] = mkt
        else:
            disabled = set(config.get("disabled_symbols", []))
            for sym in config.get("symbols", {}).get(market, []):
                if sym not in disabled:
                    symbols_to_fetch[sym] = market

        if not symbols_to_fetch:
            logger.warning(f"No symbols configured for market: {market}")
            pipeline_dict["symbols"] = {}
            pipeline_dict["pipeline_error"] = f"No symbols for {market}"
            return pipeline_dict

        # Warn if market is closed (but don't block — user explicitly requested)
        if market != "ALL" and not is_market_open(market, config):
            logger.info(f"{market} is closed but user requested scan")
            pipeline_dict["market_closed_warning"] = get_closed_markets_message(market, config)

        logger.info(f"Fetching {len(symbols_to_fetch)} symbols for {market}: {list(symbols_to_fetch)}")

        results: dict[str, dict] = {}

        with ThreadPoolExecutor(max_workers=min(5, len(symbols_to_fetch))) as executor:
            futures = {
                executor.submit(_fetch_symbol, sym, mkt, timeframes, min_bars): sym
                for sym, mkt in symbols_to_fetch.items()
            }
            for future in as_completed(futures):
                sym = futures[future]
                mkt = symbols_to_fetch[sym]
                try:
                    data = future.result(timeout=60)
                    data["market"]  = mkt
                    data["segment"] = mkt
                    data["symbol"]  = sym
                    results[sym]    = data
                    status = data.get("fetch_status", "?")
                    price  = data.get("current_price", "N/A")
                    logger.info(f"  {sym}: {status} | price={price}")
                except Exception as e:
                    logger.error(f"  {sym}: fetch failed — {e}")
                    results[sym] = {
                        "symbol": sym, "market": mkt, "segment": mkt,
                        "fetch_status": "error", "error": str(e),
                    }

        pipeline_dict["symbols"] = results
        pipeline_dict["selected_market"] = market
        logger.info(f"Bot 1 complete: {len(results)} symbols fetched")
        return pipeline_dict
