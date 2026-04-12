"""
tools/fno_scanner.py — Intraday Stock F&O signal scanner.

Scans top 15 high-liquidity NSE F&O stocks via Yahoo Finance OHLCV.
Runs the same institutional SMC pipeline as the live index engine.
Fires signals during India session (09:15–15:30 IST) only, weekdays.
Segment: INDIAN_FNO — uses correct lot sizes and charge structure.
"""
import asyncio, logging
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable, Dict, List
import httpx, pytz

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ── Top 15 high-liquidity FNO stocks ─────────────────────────────────────────
# Chosen for: tight spreads, high OI, institutional activity, Yahoo coverage
FNO_INSTRUMENTS: List[Dict] = [
    {"symbol": "RELIANCE",    "yf": "RELIANCE.NS",   "lot_size": 250,  "segment": "INDIAN_FNO"},
    {"symbol": "TCS",         "yf": "TCS.NS",         "lot_size": 150,  "segment": "INDIAN_FNO"},
    {"symbol": "INFY",        "yf": "INFY.NS",        "lot_size": 300,  "segment": "INDIAN_FNO"},
    {"symbol": "HDFCBANK",    "yf": "HDFCBANK.NS",    "lot_size": 550,  "segment": "INDIAN_FNO"},
    {"symbol": "ICICIBANK",   "yf": "ICICIBANK.NS",   "lot_size": 700,  "segment": "INDIAN_FNO"},
    {"symbol": "SBIN",        "yf": "SBIN.NS",        "lot_size": 1500, "segment": "INDIAN_FNO"},
    {"symbol": "BAJFINANCE",  "yf": "BAJFINANCE.NS",  "lot_size": 125,  "segment": "INDIAN_FNO"},
    {"symbol": "AXISBANK",    "yf": "AXISBANK.NS",    "lot_size": 625,  "segment": "INDIAN_FNO"},
    {"symbol": "TATAMOTORS",  "yf": "TATAMOTORS.NS",  "lot_size": 2375, "segment": "INDIAN_FNO"},
    {"symbol": "SUNPHARMA",   "yf": "SUNPHARMA.NS",   "lot_size": 700,  "segment": "INDIAN_FNO"},
    {"symbol": "WIPRO",       "yf": "WIPRO.NS",       "lot_size": 3000, "segment": "INDIAN_FNO"},
    {"symbol": "HCLTECH",     "yf": "HCLTECH.NS",     "lot_size": 700,  "segment": "INDIAN_FNO"},
    {"symbol": "TATASTEEL",   "yf": "TATASTEEL.NS",   "lot_size": 5500, "segment": "INDIAN_FNO"},
    {"symbol": "ONGC",        "yf": "ONGC.NS",        "lot_size": 3850, "segment": "INDIAN_FNO"},
    {"symbol": "HINDUNILVR",  "yf": "HINDUNILVR.NS",  "lot_size": 300,  "segment": "INDIAN_FNO"},
]

# Only 15m and 60m — match INTRADAY timeframe filter in signal_filter.py
TIMEFRAMES = [15, 60]


def _calc_atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    if len(closes) < 2:
        return closes[-1] * 0.002 if closes else 0.0
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
           for i in range(1, len(closes))]
    n = min(period, len(trs))
    return sum(trs[-n:]) / n if n > 0 else closes[-1] * 0.002


class FnOScanner:
    """
    Scans top FNO stocks every candle close (15m / 60m).
    Emits signals via callback — same interface as LiveDataEngine.
    """

    def __init__(self):
        self._running = False
        self._cb: Optional[Callable] = None
        self._last_sigs: Dict[str, datetime] = {}

    def set_signal_callback(self, cb: Callable):
        self._cb = cb

    def is_open(self) -> bool:
        now = datetime.now(IST)
        if now.weekday() >= 5:
            return False
        hm = now.hour * 60 + now.minute
        return 9 * 60 + 15 <= hm <= 15 * 60 + 30

    def mins_to_open(self) -> int:
        now = datetime.now(IST)
        if now.weekday() >= 5:
            return (7 - now.weekday()) * 24 * 60
        hm = now.hour * 60 + now.minute
        ohm = 9 * 60 + 15
        return ohm - hm if hm < ohm else 24 * 60 - hm + ohm

    async def _fetch_ohlcv(self, yf_sym: str, tf: int) -> Optional[dict]:
        iv = {15: "15m", 60: "60m"}[tf]
        rng = {15: "5d", 60: "30d"}[tf]
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}"
        try:
            async with httpx.AsyncClient(timeout=12.0,
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}) as c:
                r = await c.get(url, params={"interval": iv, "range": rng, "includePrePost": "false"})
            if r.status_code != 200:
                logger.debug(f"YF {yf_sym} {tf}m: HTTP {r.status_code}")
                return None
            res = r.json().get("chart", {}).get("result", [])
            if not res:
                return None
            q = res[0].get("indicators", {}).get("quote", [{}])[0]
            ops = [x or 0.0 for x in q.get("open", [])]
            his = [x or 0.0 for x in q.get("high", [])]
            los = [x or 0.0 for x in q.get("low", [])]
            cls = [x or 0.0 for x in q.get("close", [])]
            vls = [float(x) if x else 1000.0 for x in q.get("volume", [])]
            bars = [(o, h, l, c, v) for o, h, l, c, v in zip(ops, his, los, cls, vls) if c and c > 0]
            if len(bars) < 20:
                return None
            o2, h2, l2, c2, v2 = zip(*bars)
            return {"opens": list(o2), "highs": list(h2), "lows": list(l2),
                    "closes": list(c2), "volumes": list(v2)}
        except Exception as e:
            logger.debug(f"YF {yf_sym} {tf}m: {e}")
            return None

    async def _process(self, inst: dict, tf: int) -> None:
        sym = inst["symbol"]
        key = f"{sym}:{tf}"
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

        # 60-min cooldown per symbol:timeframe
        if key in self._last_sigs and (now_utc - self._last_sigs[key]).total_seconds() < 3600:
            return

        try:
            from mcp_server.tools.institutional_detector import (
                analyze_institutional_activity, detect_htf_structure,
            )
            from mcp_server.tools.decision_engine import score_decision
            from mcp_server.tools.claude_analyzer import evaluate_setup

            m = await self._fetch_ohlcv(inst["yf"], tf)
            if not m or len(m.get("closes", [])) < 20:
                logger.debug(f"FnO {sym} {tf}m: no data")
                return

            o, h, l, c, v = m["opens"], m["highs"], m["lows"], m["closes"], m["volumes"]
            cur = c[-1]

            def _sl(arr, n): return arr[-n:] if len(arr) >= n else arr

            weekly = detect_htf_structure(_sl(c, 100), _sl(h, 100), _sl(l, 100))
            daily  = detect_htf_structure(_sl(c,  50), _sl(h,  50), _sl(l,  50))
            h4     = detect_htf_structure(_sl(c,  20), _sl(h,  20), _sl(l,  20))

            swing_l = min(l[-25:-4]) if len(l) >= 25 else min(l[:-3] if len(l) > 3 else l)
            swing_h = max(h[-25:-4]) if len(h) >= 25 else max(h[:-3] if len(h) > 3 else h)

            inst_a = analyze_institutional_activity(o, h, l, c, v, swing_l, swing_h, weekly)
            if inst_a.institutional_bias == "NEUTRAL":
                return

            # Skip plain mitigated OBs — require compensating structure
            if inst_a.mitigation_block and not (inst_a.breaker_block or inst_a.propulsion_block):
                logger.debug(f"FnO {sym} {tf}m — OB mitigated, no breaker/propulsion, skip")
                return

            sd = "LONG" if inst_a.institutional_bias == "BULLISH" else "SHORT"
            ind_conf = inst_a.inducement_confirmed
            ind_lvl  = inst_a.inducement_level
            trap = inst_a.liquidity_event.value not in ("NONE",)
            ltf = False
            if len(c) >= 6:
                pt = "UP" if c[-4] > c[-6] else "DOWN"
                ct = "UP" if c[-1] > c[-3] else "DOWN"
                ltf = pt != ct
            avg_v = sum(v[-20:]) / 20 if len(v) >= 20 else v[-1]
            # FNO stocks need stronger volume confirmation — require 2× average
            vs = v[-1] > avg_v * 2.0
            now_ist = datetime.now(IST)
            hm = now_ist.hour * 60 + now_ist.minute
            kz = any(abs(hm - (hh * 60 + mm)) <= 30 for hh, mm in [(9, 15), (11, 0), (13, 30)])
            lunch = 12 * 60 <= hm <= 13 * 60 + 30
            eq = (max(h[-50:]) + min(l[-50:])) / 2 if len(h) >= 50 else (max(h) + min(l)) / 2
            poi = ("BREAKER" if inst_a.breaker_block
                   else "OB_FVG" if inst_a.propulsion_block
                   else "OB")

            logger.info(f"FnO scanning {sym} {tf}m bias={inst_a.institutional_bias} score={inst_a.total_score:.0f} inducement={ind_conf}")

            # ── Pre-score: skip Claude for clearly weak setups ────────
            pre = score_decision(
                weekly_trend=weekly, daily_structure=daily, h4_flow=h4,
                signal_direction=sd, institutional=inst_a, poi_type=poi,
                trap_confirmed=trap, ltf_choch=ltf, volume_spike=vs,
                in_discount=cur < eq, is_index=False, is_killzone=kz,
                is_session_open=not lunch,
                htf_ob_confluence=inst_a.breaker_block,
                first_touch_ob=not inst_a.mitigation_block,
                ob_already_touched=inst_a.mitigation_block,
                is_lunch_hour=lunch,
                low_volume_session=not kz and hm > 14 * 60,
                segment="INDIAN_FNO",
                inducement_confirmed=ind_conf,
            )
            if pre.score < 50:
                logger.debug(f"FnO pre-score {pre.score:.0f}<50 for {sym} {tf}m — skip Claude")
                return

            claude = await evaluate_setup(
                symbol=sym, segment="INDIAN_FNO", timeframe=tf, current_price=cur,
                closes=c, highs=h, lows=l, volumes=v,
                inst_bias=inst_a.institutional_bias, inst_score=inst_a.total_score,
                inst_evidence=inst_a.evidence, liquidity_event=inst_a.liquidity_event.value,
                breaker_block=inst_a.breaker_block, propulsion_block=inst_a.propulsion_block,
                mitigation_block=inst_a.mitigation_block, wyckoff_phase=inst_a.wyckoff_phase.value,
                weekly_trend=weekly, daily_structure=daily, h4_flow=h4,
                in_discount=cur < eq, is_killzone=kz, ltf_choch=ltf, volume_spike=vs,
                inducement_confirmed=ind_conf, inducement_level=ind_lvl,
            )

            if claude is not None:
                if not claude.send:
                    logger.info(f"Claude rejected FnO {sym} {tf}m: {claude.risk_factors}")
                    return
                sd = claude.direction
                sig_grade = claude.grade
                sig_score = claude.confidence
                sig_narrative = claude.narrative + " | ".join(claude.key_reasons[:2])
                sig_evidence = claude.key_reasons + inst_a.evidence[:2]
            else:
                if not pre.send:
                    return
                sig_grade = pre.grade
                sig_score = pre.score
                sig_narrative = pre.narrative
                sig_evidence = pre.evidence

            # ── OB-aware SL/TP ────────────────────────────────────────
            sig_type = ("BREAKER" if inst_a.breaker_block
                        else "OB_ENTRY" if inst_a.propulsion_block
                        else "OB_ENTRY" if inst_a.ob_high
                        else "INSTITUTIONAL")
            ob_h = inst_a.ob_high
            ob_l = inst_a.ob_low
            ob_m = round((ob_h + ob_l) / 2, 2) if ob_h and ob_l else None
            atr = _calc_atr(h, l, c)

            if ob_h and ob_l and ob_l < cur < ob_h * 1.02:
                raw_sl = ob_l * (1 - 0.0005) if sd == "LONG" else ob_h * (1 + 0.0005)
                sl_dist = max(abs(cur - raw_sl), atr * 0.5, cur * 0.001)
            else:
                sl_dist = max(atr * 1.5, cur * 0.002)

            entry = cur
            sl  = round(cur - sl_dist if sd == "LONG" else cur + sl_dist, 2)
            tp1 = round(cur + sl_dist       if sd == "LONG" else cur - sl_dist,       2)
            tp2 = round(cur + sl_dist * 2.0 if sd == "LONG" else cur - sl_dist * 2.0, 2)
            tp3 = round(cur + sl_dist * 3.0 if sd == "LONG" else cur - sl_dist * 3.0, 2)
            sl_pts = abs(entry - sl)

            # ── Position sizing ───────────────────────────────────────
            lv = inst["lot_size"]
            from mcp_server.config import get_settings as _gs
            _cfg = _gs()
            risk_amt = _cfg.account_size * _cfg.risk_per_trade_percent / 100
            lots = max(1, min(10, int(risk_amt / (sl_pts * lv)))) if sl_pts > 0 and lv > 0 else 1

            # ── Charges (INDIAN_FNO: ₹20 brokerage + STT + exchange) ─
            turnover = entry * lots * lv
            charges = round(20 * 2 + turnover * 0.000125 + turnover * 0.000019 + 850, 2)

            sig = {
                "instrument": f"NSE:{sym}",
                "base_symbol": sym,
                "exchange": "NSE",
                "segment": "INDIAN_FNO",
                "direction": sd,
                "timeframe": str(tf),
                "signal_type": sig_type,
                "score": sig_score,
                "grade": sig_grade,
                "entry": round(entry, 2),
                "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "sl_points": round(sl_pts, 2),
                "sl_percent": round(sl_pts / entry * 100, 3) if entry > 0 else 0,
                "rr_ratio": 2.0,
                "lots": lots,
                "lot_size": lv,
                "charges": charges,
                "net_at_tp1": round(sl_pts * lv * lots - charges, 2),
                "net_at_tp2": round(sl_pts * lv * lots * 2 - charges, 2),
                "net_at_tp3": round(sl_pts * lv * lots * 3 - charges, 2),
                "atr": round(atr, 2),
                "htf_bias": inst_a.institutional_bias,
                "htf_matches_direction": True,
                "h4_flow": h4,
                "in_discount": cur < eq,
                "liquidity_swept": inst_a.liquidity_event.value != "NONE",
                "fvg_present": inst_a.propulsion_block,
                "volume_ratio": round(v[-1] / avg_v, 2),
                "session": "INDIA",
                "trap_present": trap,
                "is_killzone": kz,
                "ltf_choch": ltf,
                "ob_already_touched": inst_a.mitigation_block,
                "ob_high": ob_h,
                "ob_low": ob_l,
                "ob_mid": ob_m,
                "close": cur,
                "in_lunch": lunch,
                "inducement_confirmed": ind_conf,
                "inducement_level": ind_lvl,
                "options_pcr": None,
                "options_oi_bias": None,
                "max_pain": None,
                "setup_type": f"{inst_a.wyckoff_phase.value}|{inst_a.liquidity_event.value}",
                "narrative": sig_narrative,
                "htf_timeframe": "4H",
                "confluences": {
                    "htf_bias": inst_a.institutional_bias,
                    "poi_type": poi,
                    "zone_type": "Discount" if cur < eq else "Premium",
                    "liquidity_swept": inst_a.liquidity_event.value != "NONE",
                    "killzone": kz,
                    "ltf_choch": ltf,
                    "inducement": ind_conf,
                },
                "institutional_evidence": inst_a.evidence,
                "decision_evidence": sig_evidence,
                "options_signal": None,
            }

            self._last_sigs[key] = now_utc
            logger.info(
                f"FnO SIGNAL: {sym} {sd} score={sig_score:.0f} grade={sig_grade} "
                f"tf={tf}m type={sig_type} entry={entry} sl={sl} tp2={tp2} "
                f"lots={lots} lot_size={lv} atr={atr:.2f}"
            )
            if self._cb:
                await self._cb(sig)

        except Exception as e:
            logger.error(f"FnO process {sym} {tf}m: {e}", exc_info=True)

    async def _scan_cycle(self) -> None:
        """One scan cycle — fetch all instruments concurrently for each timeframe."""
        now = datetime.now(IST)
        hm = now.hour * 60 + now.minute
        lunch = 12 * 60 <= hm <= 13 * 60 + 30

        if lunch:
            logger.debug("FnO scanner: lunch break, skipping cycle")
            return

        # Run all instruments concurrently but limit concurrency to avoid rate-limits
        for tf in TIMEFRAMES:
            tasks = [self._process(inst, tf) for inst in FNO_INSTRUMENTS]
            # Run in batches of 5 to be polite to Yahoo Finance
            for i in range(0, len(tasks), 5):
                await asyncio.gather(*tasks[i:i+5], return_exceptions=True)
                if i + 5 < len(tasks):
                    await asyncio.sleep(1)

    async def run(self) -> None:
        self._running = True
        logger.info(f"FnO scanner started — watching {len(FNO_INSTRUMENTS)} stocks, TF={TIMEFRAMES}")
        while self._running:
            try:
                if self.is_open():
                    await self._scan_cycle()
                    await asyncio.sleep(15 * 60)   # scan every 15 minutes
                else:
                    mins = self.mins_to_open()
                    sl = 1800 if mins > 60 else 60
                    logger.info(f"FnO scanner: market closed, opens in {mins}min")
                    await asyncio.sleep(sl)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"FnO scanner cycle: {e}", exc_info=True)
                await asyncio.sleep(60)
        self._running = False

    def stop(self):
        self._running = False


_scanner: Optional[FnOScanner] = None


def get_fno_scanner() -> FnOScanner:
    global _scanner
    if _scanner is None:
        _scanner = FnOScanner()
    return _scanner
