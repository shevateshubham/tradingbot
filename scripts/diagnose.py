"""
scripts/diagnose.py — End-to-end pipeline diagnostic.

Tests every stage:
  1. NSE cookie fetch (feeds.py)
  2. NSE live index prices
  3. NSE option chain
  4. Binance OHLCV
  5. Institutional analysis (signal scoring)
  6. Telegram bot connectivity

Run from repo root:
  python scripts/diagnose.py
"""

import asyncio
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
WARN = "WARN"

results = []

def log(stage, status, detail=""):
    tag = {"PASS": "✅", "FAIL": "❌", "SKIP": "⚠️", "WARN": "⚠️"}.get(status, "?")
    line = f"  {tag} [{status}] {stage}"
    if detail:
        line += f" — {detail}"
    print(line)
    results.append((stage, status, detail))


# ─────────────────────────────────────────────────────────────────────
# 1. Environment / config
# ─────────────────────────────────────────────────────────────────────

async def check_config():
    print("\n[1] Configuration")
    try:
        from mcp_server.config import get_settings
        s = get_settings()
        for name, val in [
            ("TELEGRAM_BOT_TOKEN",  s.telegram_bot_token),
            ("TELEGRAM_CHAT_ID",    s.telegram_chat_id),
            ("ANTHROPIC_API_KEY",   s.anthropic_api_key),
            ("TV_WEBHOOK_SECRET",   s.tv_webhook_secret),
            ("DATABASE_URL",        s.database_url),
        ]:
            if val and val not in ("", "changeme"):
                log(name, PASS, f"{str(val)[:6]}…")
            else:
                log(name, FAIL, "not set or empty")
    except Exception as e:
        log("config load", FAIL, str(e))


# ─────────────────────────────────────────────────────────────────────
# 2. NSE cookie fetch (feeds.py)
# ─────────────────────────────────────────────────────────────────────

async def check_nse_cookies():
    print("\n[2] NSE Cookie Fetch (feeds.py)")
    try:
        from mcp_server.resources.feeds import _refresh_nse_cookies
        t0 = time.time()
        cookies = await _refresh_nse_cookies()
        elapsed = round(time.time() - t0, 2)
        if cookies:
            log("NSE homepage cookies", PASS, f"{list(cookies.keys())} in {elapsed}s")
        else:
            log("NSE homepage cookies", FAIL, f"no cookies returned after {elapsed}s — WAF blocking")
    except Exception as e:
        log("NSE cookie fetch", FAIL, str(e))


# ─────────────────────────────────────────────────────────────────────
# 3. NSE live index prices
# ─────────────────────────────────────────────────────────────────────

async def check_nse_prices():
    print("\n[3] NSE Live Index Prices")
    try:
        import httpx
        from mcp_server.resources.feeds import _get_nse_cookies, NSE_HEADERS, NSE_BASE_URL
        cookies = await _get_nse_cookies()
        url = f"{NSE_BASE_URL}/api/allIndices"
        async with httpx.AsyncClient(timeout=10.0, cookies=cookies, headers=NSE_HEADERS) as c:
            r = await c.get(url)
        if r.status_code == 200:
            data = r.json().get("data", [])
            name_map = {
                "Nifty 50": "NIFTY", "NIFTY 50": "NIFTY",
                "Nifty Bank": "BANKNIFTY", "NIFTY BANK": "BANKNIFTY",
                "Nifty Fin Service": "FINNIFTY", "NIFTY FIN SERVICE": "FINNIFTY",
                "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
            }
            found = {}
            for idx in data:
                nm = idx.get("indexSymbol", idx.get("index", ""))
                key = name_map.get(nm)
                if key:
                    found[key] = float(idx.get("last", idx.get("lastPrice", 0)) or 0)
            if found:
                summary = ", ".join(f"{k}={v:.0f}" for k, v in found.items())
                log("NSE index prices", PASS, summary)
            else:
                log("NSE index prices", WARN, f"HTTP 200 but no index data matched ({len(data)} rows returned)")
        else:
            log("NSE index prices", FAIL, f"HTTP {r.status_code}")
    except Exception as e:
        log("NSE index prices", FAIL, str(e))


# ─────────────────────────────────────────────────────────────────────
# 4. NSE option chain
# ─────────────────────────────────────────────────────────────────────

async def check_nse_option_chain():
    print("\n[4] NSE Option Chain (NIFTY)")
    try:
        from mcp_server.resources.feeds import fetch_nse_option_chain
        t0 = time.time()
        data = await fetch_nse_option_chain("NIFTY", is_index=True)
        elapsed = round(time.time() - t0, 2)
        if data and "records" in data:
            strikes = len(data["records"].get("data", []))
            log("NSE option chain NIFTY", PASS, f"{strikes} strikes in {elapsed}s")
        elif data:
            log("NSE option chain NIFTY", WARN, f"got data but no 'records' key — keys: {list(data.keys())}")
        else:
            log("NSE option chain NIFTY", FAIL, f"returned None after {elapsed}s (403/timeout/empty)")
    except Exception as e:
        log("NSE option chain NIFTY", FAIL, str(e))


# ─────────────────────────────────────────────────────────────────────
# 5. Binance OHLCV
# ─────────────────────────────────────────────────────────────────────

async def check_binance():
    print("\n[5] Binance OHLCV (BTCUSDT 15m)")
    try:
        from mcp_server.resources.feeds import fetch_binance_klines
        t0 = time.time()
        data = await fetch_binance_klines("BTCUSDT", "15m", 50)
        elapsed = round(time.time() - t0, 2)
        if data and len(data) >= 20:
            last_close = float(data[-1][4])
            log("Binance OHLCV BTCUSDT", PASS, f"{len(data)} candles, last close={last_close:.2f} in {elapsed}s")
        else:
            log("Binance OHLCV BTCUSDT", FAIL, f"returned {len(data) if data else 0} candles")
    except Exception as e:
        log("Binance OHLCV BTCUSDT", FAIL, str(e))


# ─────────────────────────────────────────────────────────────────────
# 6. Institutional analysis on live Binance data
# ─────────────────────────────────────────────────────────────────────

async def check_institutional_analysis():
    print("\n[6] Institutional Analysis (BTCUSDT — live data)")
    try:
        from mcp_server.resources.feeds import fetch_binance_klines
        from mcp_server.tools.institutional_detector import analyze_institutional_activity, detect_htf_structure

        raw = await fetch_binance_klines("BTCUSDT", "15m", 100)
        if not raw or len(raw) < 30:
            log("institutional analysis", SKIP, "Binance fetch returned no data — skipping")
            return

        o, h, l, c, v = [], [], [], [], []
        for candle in raw:
            try:
                o.append(float(candle[1])); h.append(float(candle[2]))
                l.append(float(candle[3])); c.append(float(candle[4]))
                v.append(float(candle[5]))
            except Exception:
                continue

        _sl = min(l[-25:-4]) if len(l) >= 25 else min(l)
        _sh = max(h[-25:-4]) if len(h) >= 25 else max(h)
        weekly = detect_htf_structure(c[-100:], h[-100:], l[-100:])
        inst   = analyze_institutional_activity(o, h, l, c, v, _sl, _sh, weekly)

        log(
            "institutional analysis",
            PASS,
            f"bias={inst.institutional_bias} score={inst.total_score:.0f} "
            f"event={inst.liquidity_event.value} evidence={inst.evidence[:2]}"
        )
    except Exception as e:
        log("institutional analysis", FAIL, str(e))


# ─────────────────────────────────────────────────────────────────────
# 7. Claude API
# ─────────────────────────────────────────────────────────────────────

async def check_claude_api():
    print("\n[7] Claude API (evaluate_setup)")
    try:
        from mcp_server.resources.feeds import fetch_binance_klines
        from mcp_server.tools.institutional_detector import analyze_institutional_activity, detect_htf_structure
        from mcp_server.tools.claude_analyzer import evaluate_setup

        raw = await fetch_binance_klines("BTCUSDT", "15m", 60)
        if not raw or len(raw) < 30:
            log("Claude API", SKIP, "no Binance data to test with")
            return

        o, h, l, c, v = [], [], [], [], []
        for candle in raw:
            try:
                o.append(float(candle[1])); h.append(float(candle[2]))
                l.append(float(candle[3])); c.append(float(candle[4]))
                v.append(float(candle[5]))
            except Exception:
                continue

        _sl = min(l[-25:-4]) if len(l) >= 25 else min(l)
        _sh = max(h[-25:-4]) if len(h) >= 25 else max(h)
        weekly = detect_htf_structure(c, h, l)
        daily  = detect_htf_structure(c[-60:], h[-60:], l[-60:])
        h4     = detect_htf_structure(c[-20:], h[-20:], l[-20:])
        inst   = analyze_institutional_activity(o, h, l, c, v, _sl, _sh, weekly)
        eq     = (max(h[-50:]) + min(l[-50:])) / 2

        t0 = time.time()
        decision = await evaluate_setup(
            symbol="BTCUSDT", segment="CRYPTO", timeframe=15,
            current_price=c[-1], closes=c, highs=h, lows=l, volumes=v,
            inst_bias=inst.institutional_bias, inst_score=inst.total_score,
            inst_evidence=inst.evidence, liquidity_event=inst.liquidity_event.value,
            breaker_block=inst.breaker_block, propulsion_block=inst.propulsion_block,
            mitigation_block=inst.mitigation_block, wyckoff_phase=inst.wyckoff_phase.value,
            weekly_trend=weekly, daily_structure=daily, h4_flow=h4,
            in_discount=c[-1] < eq, is_killzone=False, ltf_choch=False,
            volume_spike=False, options_data=None,
        )
        elapsed = round(time.time() - t0, 2)

        if decision is not None:
            log(
                "Claude API",
                PASS,
                f"send={decision.send} grade={decision.grade} "
                f"confidence={decision.confidence:.0f} in {elapsed}s"
            )
        else:
            log("Claude API", WARN, "returned None (no API key or API error) — will use fallback scorer")
    except Exception as e:
        log("Claude API", FAIL, str(e))


# ─────────────────────────────────────────────────────────────────────
# 8. Telegram bot connectivity
# ─────────────────────────────────────────────────────────────────────

async def check_telegram():
    print("\n[8] Telegram Bot")
    try:
        import httpx
        from mcp_server.config import get_settings
        s = get_settings()
        token = s.telegram_bot_token
        url = f"https://api.telegram.org/bot{token}/getMe"
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url)
        if r.status_code == 200:
            bot = r.json().get("result", {})
            log("Telegram getMe", PASS, f"@{bot.get('username')} ({bot.get('first_name')})")
        else:
            log("Telegram getMe", FAIL, f"HTTP {r.status_code}: {r.text[:80]}")
    except Exception as e:
        log("Telegram bot", FAIL, str(e))


async def check_telegram_sendable():
    """Send a diagnostic test message to the configured chat."""
    print("\n[8b] Telegram Send Test")
    try:
        import httpx
        from mcp_server.config import get_settings
        s = get_settings()
        url = f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, json={
                "chat_id": s.telegram_chat_id,
                "text": "🔧 Diagnostic test — pipeline check running. Ignore this message.",
                "parse_mode": "HTML",
            })
        if r.status_code == 200:
            log("Telegram send message", PASS, f"chat_id={s.telegram_chat_id}")
        else:
            log("Telegram send message", FAIL, f"HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        log("Telegram send message", FAIL, str(e))


# ─────────────────────────────────────────────────────────────────────
# 9. Market scanner — does it actually fetch NSE data?
# ─────────────────────────────────────────────────────────────────────

async def check_market_scanner_nse():
    print("\n[9] Market Scanner — NSE OHLCV fetch")
    try:
        import inspect
        import mcp_server.tools.market_scanner as ms_module
        src = inspect.getsource(ms_module.MarketScanner.scan_instrument)
        has_nse_path = 'exchange=="NSE"' in src or "exchange == 'NSE'" in src or "exchange==\"NSE\"" in src
        if has_nse_path:
            log("NSE OHLCV in scanner", PASS, "NSE fetch path (Yahoo Finance fallback) present")
        else:
            log(
                "NSE OHLCV in scanner",
                FAIL,
                "scan_instrument only fetches ohlcv for exchange=BINANCE — "
                "NSE instruments (NIFTY, BANKNIFTY, FNO) always return None"
            )
        # Also test Yahoo Finance fetch directly
        result = await ms_module.fetch_nse_ohlcv("NIFTY", "15m", 50)
        if result and len(result.get("closes", [])) >= 20:
            log("Yahoo Finance NIFTY 15m", PASS, f"{len(result['closes'])} bars, last close={result['closes'][-1]:.0f}")
        else:
            log("Yahoo Finance NIFTY 15m", WARN, "returned no data (may be outside market hours)")
    except Exception as e:
        log("scanner code check", FAIL, str(e))


# ─────────────────────────────────────────────────────────────────────
# 10. Live engine NSEFetcher headers
# ─────────────────────────────────────────────────────────────────────

async def check_live_engine_headers():
    print("\n[10] Live Engine — NSEFetcher headers")
    try:
        import inspect
        import mcp_server.tools.live_data_engine as lde_module
        src = inspect.getsource(lde_module.NSEFetcher._h)
        if "sec-ch-ua" in src or "sec-fetch" in src:
            log("NSEFetcher modern headers", PASS)
        else:
            log(
                "NSEFetcher modern headers",
                FAIL,
                "live_data_engine.NSEFetcher._h() uses minimal headers "
                "(no sec-ch-ua / sec-fetch-*) — separate from feeds.py fix, will still 403"
            )
    except Exception as e:
        log("live engine header check", FAIL, str(e))


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("  Trading Bot — End-to-End Pipeline Diagnostic")
    print("=" * 60)

    await check_config()
    await check_nse_cookies()
    await check_nse_prices()
    await check_nse_option_chain()
    await check_binance()
    await check_institutional_analysis()
    await check_claude_api()
    await check_telegram()
    await check_telegram_sendable()
    await check_market_scanner_nse()
    await check_live_engine_headers()

    print("\n" + "=" * 60)
    passed  = sum(1 for _, s, _ in results if s == PASS)
    failed  = sum(1 for _, s, _ in results if s == FAIL)
    warned  = sum(1 for _, s, _ in results if s in (WARN, SKIP))
    print(f"  RESULT: {passed} passed  |  {failed} failed  |  {warned} warnings")
    print("=" * 60)

    if failed:
        print("\nFailed checks:")
        for stage, status, detail in results:
            if status == FAIL:
                print(f"  ❌ {stage}: {detail}")


if __name__ == "__main__":
    asyncio.run(main())
