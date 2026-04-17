"""
Bot 1: Data Collector Bot
Fetches OHLCV data ONLY for the selected market/segment.

Data sources:
  - NIFTY/FOREX/GOLD/CRYPTO : TradingView via tvDatafeed
      Credentials: TRADINGVIEW_USERNAME + TRADINGVIEW_PASSWORD env vars
      (TradingView paid plan gives real-time data for all markets)

  - CRYPTO fallback          : OKX via ccxt if TradingView creds not set
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

# ── TradingView symbol map ─────────────────────────────────────────────────────
# Format: config_symbol → (tv_symbol, tv_exchange)

TV_SYMBOL_MAP: dict[str, tuple[str, str]] = {
    # NIFTY F&O stocks (NSE)
    "RELIANCE.NS":  ("RELIANCE",  "NSE"),
    "HDFCBANK.NS":  ("HDFCBANK",  "NSE"),
    "TCS.NS":       ("TCS",       "NSE"),
    "INFY.NS":      ("INFY",      "NSE"),
    "ICICIBANK.NS": ("ICICIBANK", "NSE"),
    "WIPRO.NS":     ("WIPRO",     "NSE"),
    "SBIN.NS":      ("SBIN",      "NSE"),
    "AXISBANK.NS":  ("AXISBANK",  "NSE"),
    "BAJFINANCE.NS":("BAJFINANCE","NSE"),
    "KOTAKBANK.NS": ("KOTAKBANK", "NSE"),
    # Forex
    "EURUSD=X":     ("EURUSD",    "FX"),
    "GBPUSD=X":     ("GBPUSD",    "FX"),
    "USDJPY=X":     ("USDJPY",    "FX"),
    "AUDUSD=X":     ("AUDUSD",    "FX"),
    "USDCAD=X":     ("USDCAD",    "FX"),
    # Gold / Commodities
    "GC=F":         ("XAUUSD",    "OANDA"),
    "SI=F":         ("XAGUSD",    "OANDA"),
    # Crypto
    "BTC-USD":      ("BTCUSDT",   "BINANCE"),
    "ETH-USD":      ("ETHUSDT",   "BINANCE"),
    "BNB-USD":      ("BNBUSDT",   "BINANCE"),
    "SOL-USD":      ("SOLUSDT",   "BINANCE"),
}

TV_INTERVAL_MAP = {
    "1m":  "in_1_minute",
    "5m":  "in_5_minute",
    "15m": "in_15_minute",
    "1h":  "in_1_hour",
    "4h":  "in_4_hour",
    "1d":  "in_daily",
}

# ── OKX fallback for CRYPTO ───────────────────────────────────────────────────

CCXT_SYMBOL_MAP: dict[str, str] = {
    "BTC-USD": "BTC/USDT",
    "ETH-USD": "ETH/USDT",
    "BNB-USD": "BNB/USDT",
    "SOL-USD": "SOL/USDT",
}

# ── TradingView session cache ─────────────────────────────────────────────────

_TV_CLIENT = None


def _get_tv_client():
    """Return cached TvDatafeed client. Logs in with credentials if available."""
    global _TV_CLIENT
    if _TV_CLIENT is not None:
        return _TV_CLIENT

    from tvDatafeed import TvDatafeed
    username = os.environ.get("TRADINGVIEW_USERNAME", "")
    password = os.environ.get("TRADINGVIEW_PASSWORD", "")

    if username and password:
        logger.info("TvDatafeed: logging in with credentials (real-time data)")
        _TV_CLIENT = TvDatafeed(username=username, password=password)
    else:
        logger.info("TvDatafeed: no credentials, using delayed data")
        _TV_CLIENT = TvDatafeed()

    return _TV_CLIENT


# ── Fetchers ──────────────────────────────────────────────────────────────────

def _fetch_tradingview(symbol: str, timeframes: list[str], min_bars: int) -> dict:
    """Fetch OHLCV from TradingView via tvDatafeed."""
    global _TV_CLIENT
    try:
        from tvDatafeed import Interval
    except ImportError:
        return {"fetch_status": "error", "error": "tvDatafeed not installed"}

    tv_entry = TV_SYMBOL_MAP.get(symbol)
    if not tv_entry:
        return {"fetch_status": "error", "error": f"No TradingView mapping for {symbol}"}

    tv_symbol, tv_exchange = tv_entry
    tv = _get_tv_client()
    tf_data: dict = {}

    for tf in timeframes:
        interval_name = TV_INTERVAL_MAP.get(tf)
        if not interval_name:
            tf_data[tf] = None
            continue

        interval = getattr(Interval, interval_name)

        for attempt in range(3):
            try:
                df = tv.get_hist(
                    symbol   = tv_symbol,
                    exchange = tv_exchange,
                    interval = interval,
                    n_bars   = 100,
                )

                if df is None or len(df) < min_bars:
                    logger.warning(f"{symbol} {tf}: only {len(df) if df is not None else 0} bars (need {min_bars})")
                    tf_data[tf] = None
                else:
                    tf_data[tf] = {
                        "opens":      df["open"].tolist(),
                        "highs":      df["high"].tolist(),
                        "lows":       df["low"].tolist(),
                        "closes":     df["close"].tolist(),
                        "volumes":    df["volume"].tolist(),
                        "timestamps": [str(ts) for ts in df.index.tolist()],
                        "count":      len(df),
                    }
                break  # success (or insufficient data) — no retry needed

            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "Too Many Requests" in err_str:
                    wait = 3 * (2 ** attempt)  # 3s, 6s, 12s
                    logger.warning(f"{symbol} {tf} rate-limited (429), retry in {wait}s (attempt {attempt+1}/3)")
                    _TV_CLIENT = None  # force fresh WebSocket on next attempt
                    time.sleep(wait)
                    tv = _get_tv_client()
                elif attempt == 2:
                    logger.error(f"{symbol} {tf} tvDatafeed error: {e}")
                    tf_data[tf] = None
                    break
                else:
                    logger.error(f"{symbol} {tf} tvDatafeed error: {e}")
                    tf_data[tf] = None
                    break
        else:
            logger.error(f"{symbol} {tf}: all retries failed (429)")
            tf_data[tf] = None

        time.sleep(0.8)  # between timeframes within same symbol

    current_price = None
    try:
        # Use the most granular successfully-fetched timeframe for current price
        for tf_key in ["1m", "5m", "15m", "1h", "4h", "1d"]:
            closes = (tf_data.get(tf_key) or {}).get("closes")
            if closes:
                current_price = closes[-1]
                break
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
    """
    Data source routing:
    - TradingView: used when TRADINGVIEW_USERNAME + TRADINGVIEW_PASSWORD are set
    - ccxt/OKX:    fallback for CRYPTO when TV creds are absent
    - TV without creds is unreliable (delayed/blocked) so we skip it for CRYPTO
    """
    tv_creds = bool(
        os.environ.get("TRADINGVIEW_USERNAME") and
        os.environ.get("TRADINGVIEW_PASSWORD")
    )

    if tv_creds and symbol in TV_SYMBOL_MAP:
        result = _fetch_tradingview(symbol, timeframes, min_bars)
        # If TV failed for a CRYPTO symbol, fall back to ccxt immediately
        if result.get("fetch_status") == "error" and market == "CRYPTO":
            logger.info(f"{symbol}: TV failed, trying ccxt fallback")
            return _fetch_ccxt(symbol, timeframes, min_bars)
        return result

    if market == "CRYPTO":
        logger.info(f"{symbol}: no TV creds — using ccxt/OKX")
        return _fetch_ccxt(symbol, timeframes, min_bars)

    # Non-CRYPTO without creds: try TV in delayed mode (NIFTY/FOREX/GOLD)
    if symbol in TV_SYMBOL_MAP:
        return _fetch_tradingview(symbol, timeframes, min_bars)

    return {"fetch_status": "error", "error": f"No data source configured for {symbol}"}


class DataCollectorBot:
    """
    Bot 1: Fetches OHLCV data for all symbols in the selected market.
    Only fetches data for the market requested — no wasted API calls.
    """

    def run(self, pipeline_dict: dict) -> dict:
        config     = load_config()
        market     = pipeline_dict.get("selected_market", "ALL")
        tt_config  = pipeline_dict.get("trade_type_config", {})
        timeframes = tt_config.get("timeframes") or config.get("timeframes", ["1m", "5m", "15m"])
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

        if market != "ALL" and not is_market_open(market, config):
            logger.info(f"{market} is closed but user requested scan")
            pipeline_dict["market_closed_warning"] = get_closed_markets_message(market, config)

        logger.info(f"Fetching {len(symbols_to_fetch)} symbols for {market}: {list(symbols_to_fetch)}")

        results: dict[str, dict] = {}

        with ThreadPoolExecutor(max_workers=min(2, len(symbols_to_fetch))) as executor:
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
