"""
Bot 1: Data Collector Bot
Fetches OHLCV data ONLY for the selected market/segment.
Uses yfinance (NIFTY/FOREX/GOLD) and ccxt Binance public API (CRYPTO).
No API keys required.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pytz

from utils.config_loader import load_config, get_symbols_for_market, get_market_for_symbol
from utils.logger import get_logger
from utils.market_hours import is_market_open, get_closed_markets_message

logger = get_logger("bot1")

# yfinance period/interval combos for intraday data
YF_TF_PARAMS = {
    "1m":  {"period": "1d",  "interval": "1m"},
    "5m":  {"period": "5d",  "interval": "5m"},
    "15m": {"period": "5d",  "interval": "15m"},
}

# ccxt uses same notation as config
CCXT_SYMBOL_MAP = {
    "BTC-USD": "BTC/USDT",
    "ETH-USD": "ETH/USDT",
    "BNB-USD": "BNB/USDT",
}


def _fetch_yfinance(symbol: str, timeframes: list[str], min_bars: int) -> dict:
    """Fetch OHLCV from Yahoo Finance for stocks/forex/gold."""
    try:
        import yfinance as yf
    except ImportError:
        return {"fetch_status": "error", "error": "yfinance not installed"}

    tf_data = {}
    for tf in timeframes:
        params = YF_TF_PARAMS.get(tf)
        if not params:
            continue
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=params["period"], interval=params["interval"])
            if df is None or len(df) < min_bars:
                tf_data[tf] = None
                logger.warning(f"{symbol} {tf}: only {len(df) if df is not None else 0} bars (need {min_bars})")
                continue

            tf_data[tf] = {
                "opens":   df["Open"].tolist(),
                "highs":   df["High"].tolist(),
                "lows":    df["Low"].tolist(),
                "closes":  df["Close"].tolist(),
                "volumes": df["Volume"].tolist(),
                "timestamps": [str(ts) for ts in df.index.tolist()],
                "count":   len(df),
            }
        except Exception as e:
            logger.error(f"{symbol} {tf} yfinance error: {e}")
            tf_data[tf] = None

    current_price = None
    try:
        closes_15m = (tf_data.get("15m") or tf_data.get("5m") or {}).get("closes")
        if closes_15m:
            current_price = closes_15m[-1]
    except Exception:
        pass

    return {
        "timeframes":     tf_data,
        "current_price":  current_price,
        "fetch_status":   "ok" if any(v is not None for v in tf_data.values()) else "error",
        "fetched_at":     datetime.utcnow().isoformat(),
    }


def _fetch_ccxt(symbol: str, timeframes: list[str], min_bars: int) -> dict:
    """Fetch OHLCV from Binance public API via ccxt (no API key required)."""
    try:
        import ccxt
    except ImportError:
        return {"fetch_status": "error", "error": "ccxt not installed"}

    ccxt_symbol = CCXT_SYMBOL_MAP.get(symbol, symbol.replace("-", "/").replace("USD", "USDT"))

    try:
        exchange = ccxt.binance({"enableRateLimit": True})
    except Exception as e:
        return {"fetch_status": "error", "error": str(e)}

    tf_data = {}
    for tf in timeframes:
        try:
            ohlcv = exchange.fetch_ohlcv(ccxt_symbol, tf, limit=100)
            if not ohlcv or len(ohlcv) < min_bars:
                tf_data[tf] = None
                logger.warning(f"{symbol} {tf}: only {len(ohlcv)} bars from ccxt")
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
            time.sleep(0.3)  # Respect Binance rate limits
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
    return _fetch_yfinance(symbol, timeframes, min_bars)


class DataCollectorBot:
    """
    Bot 1: Fetches OHLCV data for all symbols in the selected market.
    Only fetches data for the market requested — no wasted API calls.
    """

    def run(self, pipeline_dict: dict) -> dict:
        config    = load_config()
        market    = pipeline_dict.get("selected_market", "ALL")
        timeframes = config.get("timeframes", ["1m", "5m", "15m"])
        min_bars  = config.get("min_bars_required", 30)

        symbols_to_fetch = {}
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

        # Check if market is open (warn but don't block — user explicitly requested)
        if market != "ALL":
            if not is_market_open(market, config):
                logger.info(f"{market} is closed but user requested scan")
                pipeline_dict["market_closed_warning"] = get_closed_markets_message(market, config)

        logger.info(f"Fetching {len(symbols_to_fetch)} symbols for {market}: {list(symbols_to_fetch)}")

        results: dict[str, dict] = {}

        # Parallel fetch using ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(5, len(symbols_to_fetch))) as executor:
            futures = {
                executor.submit(_fetch_symbol, sym, mkt, timeframes, min_bars): sym
                for sym, mkt in symbols_to_fetch.items()
            }
            for future in as_completed(futures):
                sym = futures[future]
                mkt = symbols_to_fetch[sym]
                try:
                    data = future.result(timeout=30)
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
