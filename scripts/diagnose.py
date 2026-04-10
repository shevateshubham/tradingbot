"""
scripts/diagnose.py — End-to-end pipeline diagnostic.
Tests every stage: config → data → analysis → scoring → Telegram
"""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"
results = []

def log(stage, status, detail=""):
    tag = {"PASS": "✅", "FAIL": "❌", "SKIP": "⚠️", "WARN": "⚠️"}.get(status, "?")
    line = f"  {tag} [{status}] {stage}"
    if detail: line += f" — {detail}"
    print(line)
    results.append((stage, status, detail))

# ─── 1. Config ───────────────────────────────────────────────────────────────
async def check_config():
    print("\n[1] Configuration")
    try:
        from mcp_server.config import get_settings
        s = get_settings()
        for name, val in [
            ("TELEGRAM_BOT_TOKEN", s.telegram_bot_token),
            ("TELEGRAM_CHAT_ID",   s.telegram_chat_id),
            ("ANTHROPIC_API_KEY",  s.anthropic_api_key),
            ("TV_WEBHOOK_SECRET",  s.tv_webhook_secret),
            ("DATABASE_URL",       s.database_url),
        ]:
            if val and val not in ("", "changeme"):
                log(name, PASS, f"{str(val)[:6]}…")
            else:
                log(name, FAIL, "not set or empty")
    except Exception as e:
        log("config load", FAIL, str(e))

# ─── 2. Binance live price + OHLCV ───────────────────────────────────────────
async def check_binance():
    print("\n[2] Binance — live price + 15m OHLCV")
    try:
        import httpx
        t0 = time.time()
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get("https://api.binance.com/api/v3/klines",
                            params={"symbol":"XAUUSDT","interval":"15m","limit":100})
        elapsed = round(time.time()-t0, 2)
        if r.status_code == 200:
            d = r.json()
            log("Binance XAUUSDT 15m", PASS,
                f"{len(d)} candles, last_close={float(d[-1][4]):.2f} in {elapsed}s")
        else:
            log("Binance XAUUSDT 15m", FAIL, f"HTTP {r.status_code}")
    except Exception as e:
        log("Binance", FAIL, str(e))

# ─── 3. Yahoo Finance — Forex 15m ────────────────────────────────────────────
async def check_yahoo_forex():
    print("\n[3] Yahoo Finance — EURUSD 15m")
    try:
        import httpx
        t0 = time.time()
        async with httpx.AsyncClient(timeout=15.0,
            headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}) as c:
            r = await c.get("https://query1.finance.yahoo.com/v8/finance/chart/EURUSD=X",
                            params={"interval":"15m","range":"5d","includePrePost":"false"})
        elapsed = round(time.time()-t0, 2)
        if r.status_code == 200:
            res = r.json().get("chart",{}).get("result",[])
            if res:
                cls = [x for x in res[0].get("indicators",{}).get("quote",[{}])[0].get("close",[]) if x]
                log("Yahoo EURUSD 15m", PASS, f"{len(cls)} bars, last={cls[-1]:.5f} in {elapsed}s")
            else:
                log("Yahoo EURUSD 15m", WARN, f"HTTP 200 but no result data")
        else:
            log("Yahoo EURUSD 15m", FAIL, f"HTTP {r.status_code}")
    except Exception as e:
        log("Yahoo Forex", FAIL, str(e))

# ─── 4. Yahoo Finance — NSE index 15m ────────────────────────────────────────
async def check_yahoo_nse():
    print("\n[4] Yahoo Finance — NIFTY 15m")
    try:
        import httpx
        t0 = time.time()
        async with httpx.AsyncClient(timeout=15.0,
            headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}) as c:
            r = await c.get("https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI",
                            params={"interval":"15m","range":"5d","includePrePost":"false"})
        elapsed = round(time.time()-t0, 2)
        if r.status_code == 200:
            res = r.json().get("chart",{}).get("result",[])
            if res:
                cls = [x for x in res[0].get("indicators",{}).get("quote",[{}])[0].get("close",[]) if x]
                log("Yahoo NIFTY 15m", PASS, f"{len(cls)} bars, last={cls[-1]:.0f} in {elapsed}s")
            else:
                log("Yahoo NIFTY 15m", WARN, "HTTP 200 but no result (market closed?)")
        else:
            log("Yahoo NIFTY 15m", FAIL, f"HTTP {r.status_code}")
    except Exception as e:
        log("Yahoo NSE", FAIL, str(e))

# ─── 5. NSE cookie + live price ──────────────────────────────────────────────
async def check_nse_live():
    print("\n[5] NSE Live Index Prices")
    try:
        from mcp_server.tools.live_data_engine import NSEFetcher
        f = NSEFetcher()
        t0 = time.time()
        prices = await f.fetch_indices()
        elapsed = round(time.time()-t0, 2)
        if prices:
            summary = ", ".join(f"{k}={v['price']:.0f}" for k,v in prices.items())
            log("NSE live prices", PASS, f"{summary} in {elapsed}s")
        else:
            log("NSE live prices", WARN, f"no data returned (403 or market closed) in {elapsed}s")
    except Exception as e:
        log("NSE live prices", FAIL, str(e))

# ─── 6. Institutional analysis — Binance data ────────────────────────────────
async def check_institutional():
    print("\n[6] Institutional Analysis (XAUUSDT 15m — live)")
    try:
        import httpx
        from mcp_server.tools.institutional_detector import analyze_institutional_activity, detect_htf_structure
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get("https://api.binance.com/api/v3/klines",
                            params={"symbol":"XAUUSDT","interval":"15m","limit":100})
        if r.status_code != 200:
            log("institutional (XAUUSDT)", SKIP, f"Binance HTTP {r.status_code}"); return
        d = r.json()
        o,h,l,c,v = [],[],[],[],[]
        for x in d:
            o.append(float(x[1])); h.append(float(x[2]))
            l.append(float(x[3])); c.append(float(x[4])); v.append(float(x[5]))
        _sl = min(l[-25:-4]); _sh = max(h[-25:-4])
        weekly = detect_htf_structure(c[-100:], h[-100:], l[-100:])
        inst = analyze_institutional_activity(o, h, l, c, v, _sl, _sh, weekly)
        log("institutional (XAUUSDT)", PASS,
            f"bias={inst.institutional_bias} score={inst.total_score:.0f} "
            f"event={inst.liquidity_event.value} wyckoff={inst.wyckoff_phase.value}")
    except Exception as e:
        log("institutional analysis", FAIL, str(e))

# ─── 7. Full engine _process simulation ──────────────────────────────────────
async def check_engine_process():
    print("\n[7] Engine _process simulation (XAUUSDT 15m — no Telegram)")
    try:
        from mcp_server.tools.evening_session_engine import EveningSessionEngine, EVENING_INSTRUMENTS
        engine = EveningSessionEngine()
        inst_info = next(i for i in EVENING_INSTRUMENTS if i["symbol"]=="XAUUSDT")
        signal_sent = []
        async def cb(sig): signal_sent.append(sig)
        engine.set_signal_callback(cb)
        t0 = time.time()
        await engine._process("XAUUSDT", 15, inst_info)
        elapsed = round(time.time()-t0, 2)
        if signal_sent:
            s = signal_sent[0]
            log("engine _process XAUUSDT 15m", PASS,
                f"SIGNAL: {s['direction']} score={s['score']} grade={s['grade']} "
                f"entry={s['entry']} sl={s['sl']} tp1={s['tp1']} in {elapsed}s")
        else:
            log("engine _process XAUUSDT 15m", WARN,
                f"no signal generated (neutral bias or score<72) in {elapsed}s — check Railway logs")
    except Exception as e:
        log("engine _process", FAIL, str(e))

# ─── 8. Claude API ───────────────────────────────────────────────────────────
async def check_claude():
    print("\n[8] Claude API (evaluate_setup)")
    try:
        import httpx
        from mcp_server.tools.institutional_detector import analyze_institutional_activity, detect_htf_structure
        from mcp_server.tools.claude_analyzer import evaluate_setup
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get("https://api.binance.com/api/v3/klines",
                            params={"symbol":"XAUUSDT","interval":"15m","limit":60})
        if r.status_code != 200:
            log("Claude API", SKIP, "no Binance data"); return
        d = r.json()
        o,h,l,c,v=[],[],[],[],[]
        for x in d:
            o.append(float(x[1])); h.append(float(x[2]))
            l.append(float(x[3])); c.append(float(x[4])); v.append(float(x[5]))
        _sl = min(l[-25:-4]); _sh = max(h[-25:-4])
        weekly = detect_htf_structure(c, h, l)
        daily = detect_htf_structure(c[-50:], h[-50:], l[-50:])
        h4 = detect_htf_structure(c[-20:], h[-20:], l[-20:])
        inst = analyze_institutional_activity(o, h, l, c, v, _sl, _sh, weekly)
        eq = (max(h[-50:]) + min(l[-50:])) / 2
        t0 = time.time()
        decision = await evaluate_setup(
            symbol="XAUUSD", segment="COMMODITY", timeframe=15,
            current_price=c[-1], closes=c, highs=h, lows=l, volumes=v,
            inst_bias=inst.institutional_bias, inst_score=inst.total_score,
            inst_evidence=inst.evidence, liquidity_event=inst.liquidity_event.value,
            breaker_block=inst.breaker_block, propulsion_block=inst.propulsion_block,
            mitigation_block=inst.mitigation_block, wyckoff_phase=inst.wyckoff_phase.value,
            weekly_trend=weekly, daily_structure=daily, h4_flow=h4,
            in_discount=c[-1]<eq, is_killzone=False, ltf_choch=False, volume_spike=False,
        )
        elapsed = round(time.time()-t0, 2)
        if decision:
            log("Claude API", PASS,
                f"send={decision.send} grade={decision.grade} confidence={decision.confidence:.0f} in {elapsed}s")
        else:
            log("Claude API", WARN, f"returned None (API key missing or error) in {elapsed}s — fallback scorer used")
    except Exception as e:
        log("Claude API", FAIL, str(e))

# ─── 9. Scoring + filter ─────────────────────────────────────────────────────
async def check_scoring():
    print("\n[9] Scoring + Signal Filter (simulated signal)")
    try:
        from mcp_server.tools.trade_scorer import score_signal
        from mcp_server.config import get_settings
        # Simulate a strong signal
        enriched = {
            "direction": "LONG", "htf_bias": "BULLISH", "signal_type": "OB_ENTRY",
            "fvg_present": True, "htf_matches_direction": True, "in_discount": True,
            "liquidity_swept": True, "is_killzone": True, "ltf_choch": True,
            "session": "LONDON", "options_confirm": False, "near_max_pain": False,
            "volume_ratio": 2.0, "in_lunch": False, "trap_present": False,
            "trap_direction": "", "segment": "FOREX", "close": 1.0850,
            "ob_already_touched": False,
        }
        result = score_signal(enriched)
        log("score_signal (strong LONG)", PASS if result.passed else WARN,
            f"score={result.score} grade={result.grade} passed={result.passed} "
            f"breakdown={result.breakdown}")
        settings = get_settings()
        log("min_confluence_score", PASS, f"{settings.min_confluence_score}")
    except Exception as e:
        log("scoring", FAIL, str(e))

# ─── 10. Telegram bot ────────────────────────────────────────────────────────
async def check_telegram():
    print("\n[10] Telegram Bot")
    try:
        import httpx
        from mcp_server.config import get_settings
        s = get_settings()
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"https://api.telegram.org/bot{s.telegram_bot_token}/getMe")
        if r.status_code == 200:
            bot = r.json().get("result", {})
            log("Telegram getMe", PASS, f"@{bot.get('username')} ({bot.get('first_name')})")
        else:
            log("Telegram getMe", FAIL, f"HTTP {r.status_code}: {r.text[:80]}")
    except Exception as e:
        log("Telegram bot", FAIL, str(e))

# ─── 11. Trade calculator ────────────────────────────────────────────────────
async def check_calculator():
    print("\n[11] Trade Calculator")
    try:
        from mcp_server.tools.trade_calculator import calculate_position
        enriched = {
            "instrument": "FOREX:EURUSD", "base_symbol": "EURUSD",
            "segment": "FOREX", "direction": "LONG",
            "signal_type": "OB_ENTRY", "close": 1.0850,
            "ob_high": 1.0865, "ob_low": 1.0840,
            "fvg_high": 1.0860, "fvg_low": 1.0845,
            "score": 82, "grade": "A",
        }
        result = calculate_position(enriched, account_size=100000, risk_percent=1.0)
        if result.valid:
            log("calculate_position (EURUSD LONG)", PASS,
                f"entry={result.entry} sl={result.sl} tp1={result.tp1} "
                f"rr={result.rr_ratio:.1f} lots={result.lots} net_tp1={result.net_at_tp1:.0f}")
        else:
            log("calculate_position (EURUSD LONG)", WARN, f"rejected: {result.reject_reason}")
    except Exception as e:
        log("trade calculator", FAIL, str(e))

# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 65)
    print("  Trading Bot — End-to-End Pipeline Diagnostic")
    print("=" * 65)
    await check_config()
    await check_binance()
    await check_yahoo_forex()
    await check_yahoo_nse()
    await check_nse_live()
    await check_institutional()
    await check_engine_process()
    await check_claude()
    await check_scoring()
    await check_telegram()
    await check_calculator()
    print("\n" + "=" * 65)
    passed = sum(1 for _,s,_ in results if s==PASS)
    failed = sum(1 for _,s,_ in results if s==FAIL)
    warned = sum(1 for _,s,_ in results if s in (WARN,SKIP))
    print(f"  RESULT: {passed} passed  |  {failed} failed  |  {warned} warnings")
    print("=" * 65)
    if failed:
        print("\nFailed checks:")
        for stage,status,detail in results:
            if status == FAIL:
                print(f"  ❌ {stage}: {detail}")

if __name__ == "__main__":
    asyncio.run(main())
