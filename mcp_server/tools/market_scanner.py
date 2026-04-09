import asyncio, logging
from datetime import datetime, date
from typing import Optional, List, Dict, Any
import httpx, pytz
from mcp_server.config import get_settings, FNO_SYMBOLS, NSE_BASE_URL, BINANCE_BASE_URL
from mcp_server.tools.institutional_detector import analyze_institutional_activity, detect_htf_structure
from mcp_server.tools.decision_engine import score_decision
from mcp_server.tools.claude_analyzer import evaluate_setup
from mcp_server.tools.options_analysis import analyze_option_chain, is_near_max_pain
from mcp_server.resources.feeds import fetch_nse_option_chain, fetch_binance_klines, _get_nse_cookies

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

SCAN_INDICES = [("NIFTY","NSE"),("BANKNIFTY","NSE"),("FINNIFTY","NSE")]
SCAN_FNO = [("RELIANCE","NSE"),("TCS","NSE"),("HDFCBANK","NSE"),("INFY","NSE"),("ICICIBANK","NSE"),("KOTAKBANK","NSE"),("AXISBANK","NSE"),("SBIN","NSE"),("BAJFINANCE","NSE"),("WIPRO","NSE")]
SCAN_CRYPTO = [("BTCUSDT","BINANCE"),("ETHUSDT","BINANCE"),("BNBUSDT","BINANCE"),("SOLUSDT","BINANCE"),("XRPUSDT","BINANCE")]

async def fetch_binance_ohlcv(symbol, interval="15m", limit=100):
    raw=await fetch_binance_klines(symbol, interval, limit)
    if not raw: return None
    o,h,l,c,v=[],[],[],[],[]
    for candle in raw:
        try: o.append(float(candle[1])); h.append(float(candle[2])); l.append(float(candle[3])); c.append(float(candle[4])); v.append(float(candle[5]))
        except: continue
    return {"opens":o,"highs":h,"lows":l,"closes":c,"volumes":v} if len(c)>=20 else None

class MarketScanner:
    def __init__(self):
        self._last_signals={}
        self._cooldown=60

    async def scan_instrument(self, symbol, exchange, segment, enabled_markets):
        key=f"{exchange}:{symbol}"
        if key in self._last_signals:
            age=(datetime.utcnow()-self._last_signals[key]).total_seconds()/60
            if age<self._cooldown: return None
        if segment not in enabled_markets: return None
        ohlcv=None
        if exchange=="BINANCE":
            ohlcv=await fetch_binance_ohlcv(symbol,"15m",100)
        if not ohlcv or len(ohlcv.get("closes",[]))<30: return None
        o=ohlcv["opens"]; h=ohlcv["highs"]; l=ohlcv["lows"]; c=ohlcv["closes"]; v=ohlcv["volumes"]
        cur=c[-1]
        def _sl(arr,n):return arr[-n:] if len(arr)>=n else arr
        weekly=detect_htf_structure(_sl(c,100),_sl(h,100),_sl(l,100))
        daily=detect_htf_structure(_sl(c,60),_sl(h,60),_sl(l,60))
        h4=detect_htf_structure(_sl(c,30),_sl(h,30),_sl(l,30))
        support=min(l[-20:]); resistance=max(h[-20:])
        _sl=min(l[-25:-4]) if len(l)>=25 else (min(l[:-3]) if len(l)>3 else min(l))
        _sh=max(h[-25:-4]) if len(h)>=25 else (max(h[:-3]) if len(h)>3 else max(h))
        inst=analyze_institutional_activity(o,h,l,c,v,_sl,_sh,weekly)
        if inst.institutional_bias=="NEUTRAL" and inst.total_score<5: return None
        direction=inst.institutional_bias
        if direction=="NEUTRAL": return None
        sig_dir="LONG" if direction=="BULLISH" else "SHORT"
        poi="BREAKER" if inst.breaker_block else "OB_FVG" if inst.propulsion_block else "OB"
        eq=(max(h[-50:])+min(l[-50:]))/2 if len(h)>=50 else (resistance+support)/2
        in_discount=cur<eq
        trap=inst.liquidity_event.value in ("SSL_SWEPT","BSL_SWEPT","IND_BULL","IND_BEAR","TURTLE_BULL","TURTLE_BEAR")
        avg_vol=sum(v[-20:])/20
        vol_spike=v[-1]>avg_vol*1.5
        ltf_choch=False
        if len(c)>=6:
            pt="UP" if c[-4]>c[-6] else "DOWN"; ct="UP" if c[-1]>c[-3] else "DOWN"
            ltf_choch=pt!=ct
        now=datetime.now(IST); hm=now.hour*60+now.minute
        kz=any(abs(hm-(h2*60+m2))<=30 for h2,m2 in [(9,15),(13,30),(18,30)])
        lunch=12*60<=hm<=13*60+30
        options_data={}
        if segment=="INDICES":
            try:
                raw_o=await fetch_nse_option_chain(symbol,is_index=True)
                if raw_o: options_data=analyze_option_chain(raw_o,cur)
            except Exception as e: logger.debug(f"Options chain {symbol}: {e}")
        pcr_v=options_data.get("pcr",1.0); mp=options_data.get("max_pain")
        # ── Claude-powered decision ────────────────────────────────
        opts_line={"pcr":pcr_v,"max_pain":mp,"options_direction":options_data.get("options_direction")} if options_data else None
        claude=await evaluate_setup(symbol=symbol,segment=segment,timeframe=15,current_price=cur,closes=c,highs=h,lows=l,volumes=v,inst_bias=inst.institutional_bias,inst_score=inst.total_score,inst_evidence=inst.evidence,liquidity_event=inst.liquidity_event.value,breaker_block=inst.breaker_block,propulsion_block=inst.propulsion_block,mitigation_block=inst.mitigation_block,wyckoff_phase=inst.wyckoff_phase.value,weekly_trend=weekly,daily_structure=daily,h4_flow=h4,in_discount=in_discount,is_killzone=kz,ltf_choch=ltf_choch,volume_spike=vol_spike,options_data=opts_line)
        if claude is not None:
            if not claude.send: logger.info(f"Claude rejected {symbol}: {claude.risk_factors}"); return None
            sig_dir=claude.direction; sig_grade=claude.grade; sig_score=claude.confidence
            sig_narrative=claude.narrative+" | ".join(claude.key_reasons[:2]); sig_evidence=claude.key_reasons+inst.evidence[:2]
        else:
            pcr_c=(sig_dir=="LONG" and options_data.get("options_direction")=="BULLISH") or (sig_dir=="SHORT" and options_data.get("options_direction")=="BEARISH")
            near_mp=mp and is_near_max_pain(cur,mp)
            gex_s=options_data.get("gex",0)>0 if sig_dir=="LONG" else options_data.get("gex",0)<0
            opts_conf=(sig_dir=="LONG" and options_data.get("options_direction")=="BEARISH") or (sig_dir=="SHORT" and options_data.get("options_direction")=="BULLISH")
            decision=score_decision(weekly_trend=weekly,daily_structure=daily,h4_flow=h4,signal_direction=sig_dir,institutional=inst,poi_type=poi,trap_confirmed=trap,ltf_choch=ltf_choch,volume_spike=vol_spike,in_discount=in_discount,pcr_confirms=pcr_c,near_max_pain=near_mp,gex_supports=gex_s,options_conflict=opts_conf,is_index=(segment=="INDICES"),is_killzone=kz,is_session_open=kz,htf_ob_confluence=inst.breaker_block,first_touch_ob=not inst.mitigation_block,ob_already_touched=inst.mitigation_block,is_lunch_hour=lunch,low_volume_session=not kz and hm>15*60,segment=segment)
            if not decision.send: return None
            sig_grade=decision.grade; sig_score=decision.score; sig_narrative=decision.narrative; sig_evidence=decision.evidence
        options_signal=None
        if segment=="INDICES" and options_data:
            from mcp_server.tools.options_strategy import select_strategy
            try:
                dte=max(1,(3-date.today().weekday())%7 or 7)
                options_signal=select_strategy(underlying_price=cur,max_pain=mp or cur,pcr=pcr_v or 1.0,ce_walls=options_data.get("ce_walls",[]),pe_walls=options_data.get("pe_walls",[]),days_to_expiry=dte,instrument=symbol,directional_bias=direction)
            except Exception as e: logger.debug(f"Options strategy {symbol}: {e}")
        spread=cur*0.005
        entry=cur; sl=cur-spread if sig_dir=="LONG" else cur+spread
        tp1=cur+spread if sig_dir=="LONG" else cur-spread
        tp2=cur+spread*2 if sig_dir=="LONG" else cur-spread*2
        tp3=cur+spread*3 if sig_dir=="LONG" else cur-spread*3
        sl_pts=abs(entry-sl)
        self._last_signals[key]=datetime.utcnow()
        logger.info(f"SIGNAL: {symbol} {sig_dir} score={sig_score} grade={sig_grade}")
        return {"instrument":f"{exchange}:{symbol}","base_symbol":symbol,"exchange":exchange,"segment":segment,"direction":sig_dir,"timeframe":"15","signal_type":"INSTITUTIONAL","score":sig_score,"grade":sig_grade,"entry":round(entry,2),"sl":round(sl,2),"tp1":round(tp1,2),"tp2":round(tp2,2),"tp3":round(tp3,2),"sl_points":round(sl_pts,2),"sl_percent":round(sl_pts/entry*100,3),"lots":1,"charges":850.0 if segment in ("INDICES","INDIAN_FNO") else 0,"net_at_tp1":round(sl_pts*50-850,2),"htf_bias":direction,"in_discount":in_discount,"liquidity_swept":inst.liquidity_event.value!="NONE","fvg_present":inst.propulsion_block,"volume_ratio":round(v[-1]/avg_vol,2),"session":"INDIA" if segment in ("INDICES","INDIAN_FNO","INDIAN_STOCKS") else "GLOBAL","trap_present":trap,"is_killzone":kz,"ltf_choch":ltf_choch,"options_pcr":pcr_v,"options_oi_bias":options_data.get("options_direction"),"max_pain":mp,"gex":options_data.get("gex"),"setup_type":f"Institutional {inst.wyckoff_phase.value}|{inst.liquidity_event.value}","narrative":sig_narrative,"htf_timeframe":"4H","confluences":{"htf_bias":direction,"poi_type":poi,"zone_type":"Discount" if in_discount else "Premium","liquidity_swept":inst.liquidity_event.value!="NONE","killzone":kz,"ltf_choch":ltf_choch},"institutional_evidence":inst.evidence,"decision_evidence":sig_evidence,"options_signal":options_signal}

    async def run_full_scan(self, enabled_markets):
        signals=[]; tasks=[]
        now=datetime.now(IST); hm=now.hour*60+now.minute
        mo=9*60+15<=hm<=15*60+30; wd=now.weekday()<5
        if mo and wd and any(m in enabled_markets for m in ("INDICES","INDIAN_FNO")):
            if "INDICES" in enabled_markets:
                for s,e in SCAN_INDICES: tasks.append(self.scan_instrument(s,e,"INDICES",enabled_markets))
            if "INDIAN_FNO" in enabled_markets:
                for s,e in SCAN_FNO: tasks.append(self.scan_instrument(s,e,"INDIAN_FNO",enabled_markets))
        if "CRYPTO" in enabled_markets:
            for s,e in SCAN_CRYPTO: tasks.append(self.scan_instrument(s,e,"CRYPTO",enabled_markets))
        if tasks:
            results=await asyncio.gather(*tasks,return_exceptions=True)
            for r in results:
                if isinstance(r,dict) and r: signals.append(r)
        logger.info(f"Scan done: {len(tasks)} scanned, {len(signals)} signals")
        return signals

_scanner=None
def get_scanner():
    global _scanner
    if _scanner is None: _scanner=MarketScanner()
    return _scanner
