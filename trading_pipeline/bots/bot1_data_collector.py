"""
Bot 1: Data Collector Bot
Fetches OHLCV data ONLY for the selected market/segment.

Data sources:
  - NIFTY/FOREX/GOLD : Yahoo Finance v8 API (direct HTTP with cookie+crumb)
  - CRYPTO           : OKX via ccxt (no API key, globally accessible)

The direct Yahoo Finance approach bypasses yfinance's Ticker.history() which
fails on cloud IPs due to Yahoo's consent cookie requirement.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from utils.config_loader import load_config
from utils.logger import get_logger
from utils.market_hours import is_market_open, get_closed_markets_message

logger = get_logger("bot1")

# How many days back to fetch per timeframe
YF_PERIOD_DAYS = {
    "1m":  1,
    "5m":  5,
    "15m": 5,
}

# OKX symbol format (ccxt standard)
CCXT_SYMBOL_MAP = {
    "BTC-USD": "BTC/USDT",
    "ETH-USD": "ETH/USDT",
    "BNB-USD": "BNB/USDT",
    "SOL-USD": "SOL/USDT",
}

# ── Yahoo Finance session cache (one session shared across all calls) ──────────

_YF_LOCK         = threading.Lock()
_YF_REQ_SEM      = threading.Semaphore(1)   # One Yahoo Finance request at a time
_YF_LAST_REQ_TS  = 0.0
_YF_MIN_INTERVAL = 0.4                       # Seconds between requests
_YF_SESSION      = None
_YF_CRUMB: str | None = None                 # None = never fetched; "" = fetched but empty
_YF_SESSION_TS   = 0.0
_YF_SESSION_TTL  = 3600                      # Refresh session every hour


def _get_yf_session():
    """
    Return a cached (session, crumb) pair for Yahoo Finance.
    Uses `_YF_CRUMB is not None` so an empty crumb string is still cached
    (prevents all parallel threads from each triggering a refresh).
    """
    global _YF_SESSION, _YF_CRUMB, _YF_SESSION_TS

    with _YF_LOCK:
        if _YF_SESSION is not None and _YF_CRUMB is not None \
                and (time.time() - _YF_SESSION_TS) < _YF_SESSION_TTL:
            return _YF_SESSION, _YF_CRUMB

        import requests
        s = requests.Session()
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) "
                "Gecko/20100101 Firefox/121.0"
            ),
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT":             "1",
            "Connection":      "keep-alive",
        })

        # Visit Yahoo Finance to pick up consent cookies
        for url in ["https://fc.yahoo.com", "https://finance.yahoo.com"]:
            try:
                s.get(url, timeout=8)
            except Exception:
                pass

        # Fetch crumb (required for authenticated chart API calls)
        crumb = ""
        for endpoint in [
            "https://query1.finance.yahoo.com/v1/test/getcrumb",
            "https://query2.finance.yahoo.com/v1/test/getcrumb",
        ]:
            try:
                r = s.get(endpoint, timeout=8)
                if r.status_code == 200 and r.text and len(r.text) < 50:
                    crumb = r.text.strip()
                    break
            except Exception:
                pass

        _YF_SESSION    = s
        _YF_CRUMB      = crumb
        _YF_SESSION_TS = time.time()
        logger.info(f"Yahoo Finance session refreshed (crumb={'ok' if crumb else 'empty'})")
        return s, crumb


def _fetch_yfinance(symbol: str, timeframes: list[str], min_bars: int) -> dict:
    """
    Fetch OHLCV from Yahoo Finance v8 chart API.
    Uses direct HTTP with consent cookies + crumb — reliable from cloud IPs.
    """
    session, crumb = _get_yf_session()
    tf_data: dict = {}

    for tf in timeframes:
        period_days = YF_PERIOD_DAYS.get(tf, 5)
        end_ts   = int(time.time())
        start_ts = int((datetime.utcnow() - timedelta(days=period_days)).timestamp())

        params: dict = {
            "period1":        start_ts,
            "period2":        end_ts,
            "interval":       tf,
            "includePrePost": "false",
            "events":         "history",
        }
        if crumb:
            params["crumb"] = crumb

        try:
            # Serialise all Yahoo Finance requests + enforce minimum interval
            with _YF_REQ_SEM:
                global _YF_LAST_REQ_TS
                elapsed = time.time() - _YF_LAST_REQ_TS
                if elapsed < _YF_MIN_INTERVAL:
                    time.sleep(_YF_MIN_INTERVAL - elapsed)

                resp = session.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                    params=params,
                    timeout=15,
                )
                _YF_LAST_REQ_TS = time.time()

            # Process response outside the semaphore
            if resp.status_code != 200:
                logger.warning(f"{symbol} {tf}: HTTP {resp.status_code} from Yahoo")
                tf_data[tf] = None
                continue

            data   = resp.json()
            result = data.get("chart", {}).get("result") or []
            if not result:
                err = data.get("chart", {}).get("error", {})
                logger.warning(f"{symbol} {tf}: no result — {err}")
                tf_data[tf] = None
                continue

            chart      = result[0]
            timestamps = chart.get("timestamp") or []
            quote      = (chart.get("indicators", {}).get("quote") or [{}])[0]

            opens   = quote.get("open",   [])
            highs   = quote.get("high",   [])
            lows    = quote.get("low",    [])
            closes  = quote.get("close",  [])
            volumes = quote.get("volume", [])

            # Drop bars where any OHLC value is None (market gaps / pre-open slots)
            rows = [
                (t, o, h, l, c, v or 0)
                for t, o, h, l, c, v in zip(timestamps, opens, highs, lows, closes, volumes)
                if None not in (o, h, l, c)
            ]

            if len(rows) < min_bars:
                logger.warning(f"{symbol} {tf}: only {len(rows)} valid bars (need {min_bars})")
                tf_data[tf] = None
                continue

            tf_data[tf] = {
                "opens":      [r[1] for r in rows],
                "highs":      [r[2] for r in rows],
                "lows":       [r[3] for r in rows],
                "closes":     [r[4] for r in rows],
                "volumes":    [r[5] for r in rows],
                "timestamps": [str(datetime.utcfromtimestamp(r[0])) for r in rows],
                "count":      len(rows),
            }

        except Exception as e:
            logger.error(f"{symbol} {tf} fetch error: {e}")
            tf_data[tf] = None

    current_price = None
    try:
        closes_data = (tf_data.get("15m") or tf_data.get("5m") or tf_data.get("1m") or {}).get("closes")
        if closes_data:
            current_price = closes_data[-1]
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
    return _fetch_yfinance(symbol, timeframes, min_bars)


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
