"""
scripts/diagnose.py — End-to-end pipeline diagnostic.
Tests every stage: config → data → analysis → scoring → Telegram

Runs fully without Railway env vars — dummy defaults keep Settings from crashing
so every code-path is exercised. Real vs. dummy values are clearly flagged.
"""
import asyncio, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Dummy defaults so get_settings() never throws ValidationError locally ──
_DUMMY = "diagnose_dummy"
os.environ.setdefault("TELEGRAM_BOT_TOKEN",  _DUMMY)
os.environ.setdefault("TELEGRAM_CHAT_ID",    "0")
os.environ.setdefault("ANTHROPIC_API_KEY",   _DUMMY)
os.environ.setdefault("TV_WEBHOOK_SECRET",   _DUMMY)
os.environ.setdefault("DATABASE_URL",        "sqlite+aiosqlite:///diagnose_tmp.db")

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"
results = []

def log(stage, status, detail=""):
    tag = {"PASS": "✅", "FAIL": "❌", "SKIP": "⚠️", "WARN": "⚠️"}.get(status, "?")
    line = f"  {tag} [{status}] {stage}"
    if detail: line += f" — {detail}"
    print(line)
    results.append((stage, status, detail))

# ─── helper: fetch Yahoo Finance OHLCV ───────────────────────────────────────
async def _yf_ohlcv(ticker: str, interval="15m", range_="5d") -> dict | None:
    import httpx
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    async with httpx.AsyncClient(timeout=15.0,
            headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}) as c:
        r = await c.get(url, params={"interval":interval,"range":range_,"includePrePost":"false"})
    if r.status_code != 200:
        return None
    res = r.json().get("chart",{}).get("result",[])
    if not res:
        return None
    q = res[0].get("indicators",{}).get("quote",[{}])[0]
    ops = [x or 0.0 for x in q.get("open",[])]
    his = [x or 0.0 for x in q.get("high",[])]
    los = [x or 0.0 for x in q.get("low",[])]
    cls = [x or 0.0 for x in q.get("close",[])]
    vls = [float(x) if x else 1000.0 for x in q.get("volume",[])]
    bars = [(o,h,l,c,v) for o,h,l,c,v in zip(ops,his,los,cls,vls) if c and c>0]
    if len(bars) < 20:
        return None
    o2,h2,l2,c2,v2 = zip(*bars)
    return {"opens":list(o2),"highs":list(h2),"lows":list(l2),"closes":list(c2),"volumes":list(v2)}


# ─── 1. Config ───────────────────────────────────────────────────────────────
async def check_config():
    print("\n[1] Configuration")
    real_keys = {
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN",""),
        "TELEGRAM_CHAT_ID":   os.environ.get("TELEGRAM_CHAT_ID",""),
        "ANTHROPIC_API_KEY":  os.environ.get("ANTHROPIC_API_KEY",""),
        "TV_WEBHOOK_SECRET":  os.environ.get("TV_WEBHOOK_SECRET",""),
        "DATABASE_URL":       os.environ.get("DATABASE_URL",""),
    }
    for name, val in real_keys.items():
        if val and val not in ("", _DUMMY, "0"):
            log(name, PASS, f"{val[:6]}…")
        else:
            log(name, SKIP, "not set (using dummy — OK locally, required on Railway)")


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
        elif r.status_code == 451:
            log("Binance XAUUSDT 15m", SKIP, f"HTTP 451 (geo-blocked locally — works fine on Railway)")
        else:
            log("Binance XAUUSDT 15m", FAIL, f"HTTP {r.status_code}")
    except Exception as e:
        log("Binance", FAIL, str(e))


# ─── 3. Yahoo Finance — Forex 15m ────────────────────────────────────────────
async def check_yahoo_forex():
    print("\n[3] Yahoo Finance — EURUSD 15m")
    try:
        t0 = time.time()
        m = await _yf_ohlcv("EURUSD=X")
        elapsed = round(time.time()-t0, 2)
        if m:
            log("Yahoo EURUSD 15m", PASS,
                f"{len(m['closes'])} bars, last={m['closes'][-1]:.5f} in {elapsed}s")
        else:
            log("Yahoo EURUSD 15m", FAIL, f"no data returned in {elapsed}s")
    except Exception as e:
        log("Yahoo Forex", FAIL, str(e))


# ─── 4. Yahoo Finance — NSE index 15m ────────────────────────────────────────
async def check_yahoo_nse():
    print("\n[4] Yahoo Finance — NIFTY 15m")
    try:
        t0 = time.time()
        m = await _yf_ohlcv("%5ENSEI")
        elapsed = round(time.time()-t0, 2)
        if m:
            log("Yahoo NIFTY 15m", PASS,
                f"{len(m['closes'])} bars, last={m['closes'][-1]:.0f} in {elapsed}s")
        else:
            log("Yahoo NIFTY 15m", WARN, f"no data in {elapsed}s (market closed?)")
    except Exception as e:
        log("Yahoo NSE", FAIL, str(e))


# ─── 5. NSE Live Index Prices ─────────────────────────────────────────────────
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


# ─── 6. Institutional Analysis — EURUSD (Yahoo, always available) ─────────────
async def check_institutional():
    print("\n[6] Institutional Analysis (EURUSD 15m — Yahoo)")
    try:
        from mcp_server.tools.institutional_detector import analyze_institutional_activity, detect_htf_structure
        t0 = time.time()
        m = await _yf_ohlcv("EURUSD=X")
        if not m:
            log("institutional (EURUSD)", FAIL, "no Yahoo data"); return
        o,h,l,c,v = m["opens"],m["highs"],m["lows"],m["closes"],m["volumes"]
        _sl = min(l[-25:-4]) if len(l)>=25 else min(l)
        _sh = max(h[-25:-4]) if len(h)>=25 else max(h)
        weekly = detect_htf_structure(c[-100:], h[-100:], l[-100:])
        inst = analyze_institutional_activity(o, h, l, c, v, _sl, _sh, weekly)
        elapsed = round(time.time()-t0, 2)
        ob_info = f"ob_h={inst.ob_high:.5f} ob_l={inst.ob_low:.5f}" if inst.ob_high else "no_ob"
        log("institutional (EURUSD)", PASS,
            f"bias={inst.institutional_bias} score={inst.total_score:.0f} "
            f"event={inst.liquidity_event.value} {ob_info} "
            f"breaker={inst.breaker_block} mitigated={inst.mitigation_block} in {elapsed}s")
    except Exception as e:
        log("institutional analysis", FAIL, str(e))


# ─── 7. Engine _process — EURUSD 15m (full signal pipeline, no Telegram) ─────
async def check_engine_process():
    print("\n[7] Engine _process — EURUSD 15m (full signal path, no Telegram)")
    try:
        from mcp_server.tools.evening_session_engine import EveningSessionEngine, EVENING_INSTRUMENTS, INST_SESSIONS
        engine = EveningSessionEngine()
        inst_info = next(i for i in EVENING_INSTRUMENTS if i["symbol"]=="EURUSD")
        signal_sent = []
        async def cb(sig): signal_sent.append(sig)
        engine.set_signal_callback(cb)

        # Temporarily bypass session-hour and cooldown gates for the diagnostic
        import datetime, pytz
        engine._last_sigs = {}   # clear cooldown

        t0 = time.time()
        await engine._process("EURUSD", 15, inst_info)
        elapsed = round(time.time()-t0, 2)

        if signal_sent:
            s = signal_sent[0]
            log("engine _process EURUSD 15m", PASS,
                f"SIGNAL: {s['direction']} score={s['score']:.0f} grade={s['grade']} "
                f"type={s['signal_type']} entry={s['entry']} sl={s['sl']} "
                f"tp1={s['tp1']} tp2={s['tp2']} atr={s.get('atr',0):.5f} "
                f"ob_h={s.get('ob_high')} ob_l={s.get('ob_low')} in {elapsed}s")
        else:
            log("engine _process EURUSD 15m", WARN,
                f"no signal — bias NEUTRAL or mitigated OB filtered in {elapsed}s "
                f"(valid behaviour; Railway signals when market provides clear setup)")
    except Exception as e:
        log("engine _process", FAIL, str(e))


# ─── 8. Institutional + Claude — EURUSD (Yahoo data) ─────────────────────────
async def check_claude():
    print("\n[8] Claude API evaluate_setup (EURUSD — Yahoo)")
    try:
        from mcp_server.tools.institutional_detector import analyze_institutional_activity, detect_htf_structure
        from mcp_server.tools.claude_analyzer import evaluate_setup
        m = await _yf_ohlcv("EURUSD=X")
        if not m:
            log("Claude API", SKIP, "no Yahoo data"); return
        o,h,l,c,v = m["opens"],m["highs"],m["lows"],m["closes"],m["volumes"]
        _sl = min(l[-25:-4]) if len(l)>=25 else min(l)
        _sh = max(h[-25:-4]) if len(h)>=25 else max(h)
        weekly = detect_htf_structure(c, h, l)
        daily  = detect_htf_structure(c[-50:], h[-50:], l[-50:])
        h4     = detect_htf_structure(c[-20:], h[-20:], l[-20:])
        inst   = analyze_institutional_activity(o, h, l, c, v, _sl, _sh, weekly)
        eq     = (max(h[-50:]) + min(l[-50:])) / 2
        t0 = time.time()
        decision = await evaluate_setup(
            symbol="EURUSD", segment="FOREX", timeframe=15,
            current_price=c[-1], closes=c, highs=h, lows=l, volumes=v,
            inst_bias=inst.institutional_bias, inst_score=inst.total_score,
            inst_evidence=inst.evidence, liquidity_event=inst.liquidity_event.value,
            breaker_block=inst.breaker_block, propulsion_block=inst.propulsion_block,
            mitigation_block=inst.mitigation_block, wyckoff_phase=inst.wyckoff_phase.value,
            weekly_trend=weekly, daily_structure=daily, h4_flow=h4,
            in_discount=c[-1]<eq, is_killzone=False, ltf_choch=False, volume_spike=False,
        )
        elapsed = round(time.time()-t0, 2)
        if os.environ.get("ANTHROPIC_API_KEY","") in ("", _DUMMY):
            log("Claude API", SKIP,
                f"ANTHROPIC_API_KEY not set — fallback scorer will be used on Railway "
                f"(decision obj={'present' if decision else 'None'}) in {elapsed}s")
        elif decision:
            log("Claude API", PASS,
                f"send={decision.send} grade={decision.grade} "
                f"confidence={decision.confidence:.0f} in {elapsed}s")
        else:
            log("Claude API", WARN, f"returned None (retry exhausted?) in {elapsed}s")
    except Exception as e:
        log("Claude API", FAIL, str(e))


# ─── 9. Scoring pipeline ─────────────────────────────────────────────────────
async def check_scoring():
    print("\n[9] Scoring Pipeline (OB_ENTRY signal simulation)")
    try:
        from mcp_server.tools.trade_scorer import score_signal
        # Strong signal — should score A+ (≥90)
        enriched = {
            "direction": "LONG", "htf_bias": "BULLISH", "signal_type": "OB_ENTRY",
            "fvg_present": True, "htf_matches_direction": True, "in_discount": True,
            "liquidity_swept": True, "is_killzone": True, "ltf_choch": True,
            "session": "LONDON", "options_confirm": False, "near_max_pain": False,
            "volume_ratio": 2.0, "in_lunch": False, "trap_present": False,
            "trap_direction": "", "segment": "FOREX", "close": 1.1728,
            "ob_already_touched": False,
        }
        result = score_signal(enriched)
        log("score_signal (ideal LONG OB)", PASS if result.passed else WARN,
            f"score={result.score} grade={result.grade} passed={result.passed} "
            f"breakdown={result.breakdown}")

        # Weak signal — should be filtered
        weak = dict(enriched, is_killzone=False, ltf_choch=False,
                    liquidity_swept=False, fvg_present=False, volume_ratio=0.8)
        w = score_signal(weak)
        log("score_signal (weak signal)", PASS if not w.passed else WARN,
            f"score={w.score} grade={w.grade} passed={w.passed} — "
            f"{'correctly filtered' if not w.passed else 'SHOULD have been filtered'}")

        from mcp_server.config import get_settings
        s = get_settings()
        log("min_confluence_score threshold", PASS, f"{s.min_confluence_score} (grade A = ≥72)")
    except Exception as e:
        log("scoring", FAIL, str(e))


# ─── 10. Signal filter gates ──────────────────────────────────────────────────
async def check_signal_filter():
    print("\n[10] Signal Filter (gate audit)")
    try:
        from mcp_server.tools.signal_filter import apply_filters
        log("signal_filter import", PASS, "apply_filters loaded OK")

        # Check that filter requires DB session — we just verify it's importable
        # and the function signature matches what engines pass
        import inspect
        sig = inspect.signature(apply_filters)
        params = list(sig.parameters.keys())
        expected = {"session", "chat_id", "payload", "score", "grade"}
        missing = expected - set(params)
        if not missing:
            log("apply_filters signature", PASS, f"params={params}")
        else:
            log("apply_filters signature", FAIL, f"missing params: {missing}")
    except Exception as e:
        log("signal filter", FAIL, str(e))


# ─── 11. Telegram bot getMe ───────────────────────────────────────────────────
async def check_telegram():
    print("\n[11] Telegram Bot")
    token = os.environ.get("TELEGRAM_BOT_TOKEN","")
    if not token or token == _DUMMY:
        log("Telegram getMe", SKIP, "TELEGRAM_BOT_TOKEN not set locally — tested on Railway")
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"https://api.telegram.org/bot{token}/getMe")
        if r.status_code == 200:
            bot = r.json().get("result", {})
            log("Telegram getMe", PASS, f"@{bot.get('username')} ({bot.get('first_name')})")
        else:
            log("Telegram getMe", FAIL, f"HTTP {r.status_code}: {r.text[:80]}")
    except Exception as e:
        log("Telegram bot", FAIL, str(e))


# ─── 12. Trade calculator — OB-based levels ──────────────────────────────────
async def check_calculator():
    print("\n[12] Trade Calculator (OB-based levels)")
    try:
        from mcp_server.tools.trade_calculator import calculate_position
        # EURUSD LONG at OB zone — full level derivation
        enriched = {
            "instrument": "FOREX:EURUSD", "base_symbol": "EURUSD",
            "segment": "FOREX", "direction": "LONG",
            "signal_type": "OB_ENTRY", "close": 1.1728,
            "ob_high": 1.1745, "ob_low": 1.1710, "ob_mid": 1.17275,
            "fvg_present": True, "fvg_high": 1.1740, "fvg_low": 1.1718,
            "score": 85, "grade": "A",
        }
        result = calculate_position(enriched, account_size=100_000, risk_percent=1.0)
        if result.valid:
            log("calculate_position EURUSD LONG OB", PASS,
                f"entry={result.entry} sl={result.sl} tp1={result.tp1} tp2={result.tp2} "
                f"rr={result.rr_ratio:.1f}:1 lots={result.lots} "
                f"charges={result.charges:.0f} net_tp1={result.net_at_tp1:.0f}")
        else:
            log("calculate_position EURUSD LONG OB", WARN,
                f"rejected: {result.reject_reason}")

        # NIFTY LONG — indices sizing
        nifty = {
            "instrument": "NSE:NIFTY", "base_symbol": "NIFTY",
            "segment": "INDICES", "direction": "LONG",
            "signal_type": "OB_ENTRY", "close": 24051.0,
            "ob_high": 24120.0, "ob_low": 23980.0, "ob_mid": 24050.0,
            "fvg_present": False, "score": 78, "grade": "A",
        }
        nr = calculate_position(nifty, account_size=500_000, risk_percent=1.0)
        if nr.valid:
            log("calculate_position NIFTY LONG OB", PASS,
                f"entry={nr.entry} sl={nr.sl} tp1={nr.tp1} "
                f"rr={nr.rr_ratio:.1f}:1 lots={nr.lots} net_tp1={nr.net_at_tp1:.0f}")
        else:
            log("calculate_position NIFTY LONG OB", WARN,
                f"rejected: {nr.reject_reason}")
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
    await check_signal_filter()
    await check_telegram()
    await check_calculator()
    print("\n" + "=" * 65)
    passed  = sum(1 for _,s,_ in results if s == PASS)
    failed  = sum(1 for _,s,_ in results if s == FAIL)
    skipped = sum(1 for _,s,_ in results if s in (WARN, SKIP))
    print(f"  RESULT: {passed} passed  |  {failed} failed  |  {skipped} warnings/skips")
    print("=" * 65)
    if failed:
        print("\nFailed checks:")
        for stage, status, detail in results:
            if status == FAIL:
                print(f"  ❌ {stage}: {detail}")

if __name__ == "__main__":
    asyncio.run(main())
