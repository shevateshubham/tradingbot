import asyncio,logging
from collections import deque
from datetime import datetime,date,timezone
from typing import Optional,Dict,List,Any
import httpx,pytz
logger=logging.getLogger(__name__)
IST=pytz.timezone("Asia/Kolkata")
EVENING_INSTRUMENTS=[
    {"symbol":"XAUUSDT","exchange":"BINANCE","segment":"COMMODITY","display":"XAUUSD","pip":0.01,"lot_size":1},
    {"symbol":"EURUSD","exchange":"FOREX","segment":"FOREX","display":"EURUSD","pip":0.0001,"lot_size":1},
    {"symbol":"GBPUSD","exchange":"FOREX","segment":"FOREX","display":"GBPUSD","pip":0.0001,"lot_size":1},
    {"symbol":"USDJPY","exchange":"FOREX","segment":"FOREX","display":"USDJPY","pip":0.01,"lot_size":1},
    {"symbol":"GBPJPY","exchange":"FOREX","segment":"FOREX","display":"GBPJPY","pip":0.01,"lot_size":1},
    {"symbol":"AUDUSD","exchange":"FOREX","segment":"FOREX","display":"AUDUSD","pip":0.0001,"lot_size":1},
]
SESSION_START_H,SESSION_START_M=15,30
# Only 15m and 60m — pass the INTRADAY timeframe filter
TIMEFRAMES=[15,60]
BINANCE_KLINES="https://api.binance.com/api/v3/klines"
KILLZONES=[(13,30,14,0),(18,30,19,0),(20,0,20,30)]
TF_BINANCE={15:"15m",60:"1h"}
TF_YAHOO_INTERVAL={15:"15m",60:"60m"}
TF_YAHOO_RANGE={15:"5d",60:"30d"}
YF_FOREX_SYM={"EURUSD":"EURUSD=X","GBPUSD":"GBPUSD=X","USDJPY":"USDJPY=X","GBPJPY":"GBPJPY=X","AUDUSD":"AUDUSD=X"}
# Active liquidity windows per instrument (IST minutes) — suppress off-session signals
INST_SESSIONS={
    "XAUUSDT": [(13*60+30, 23*60)],   # Gold: London + NY
    "EURUSD":  [(13*60+30, 23*60)],   # Euro: London + NY
    "GBPUSD":  [(13*60+30, 23*60)],   # Cable: London + NY
    "USDJPY":  [(15*60+30, 23*60)],   # Yen: evening session only
    "GBPJPY":  [(13*60+30, 23*60)],   # GBP/JPY: London + NY
    "AUDUSD":  [(15*60+30, 23*60)],   # Aussie: evening session only
}
def _calc_atr(highs,lows,closes,period=14):
    """Average True Range — volatility measure for institutional SL sizing."""
    if len(closes)<2: return closes[-1]*0.002 if closes else 0.0
    trs=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]
    n=min(period,len(trs))
    return sum(trs[-n:])/n if n>0 else closes[-1]*0.002
class Candle:
    __slots__=["ts","open","high","low","close","volume","closed"]
    def __init__(self,ts,o): self.ts=ts;self.open=o;self.high=o;self.low=o;self.close=o;self.volume=0.0;self.closed=False
    def update(self,p,v=0): self.high=max(self.high,p);self.low=min(self.low,p);self.close=p;self.volume+=v
class CandleBuilder:
    def __init__(self,sym,tf,mx=200): self.sym=sym;self.tf=tf;self.candles=deque(maxlen=mx);self._cur=None
    def _start(self,ts):
        tm=ts.hour*60+ts.minute;cs=(tm//self.tf)*self.tf
        return ts.replace(hour=cs//60,minute=cs%60,second=0,microsecond=0)
    def update(self,price,ts,vol=0):
        cts=self._start(ts);closed=None
        if self._cur is None: self._cur=Candle(cts,price)
        elif cts>self._cur.ts: self._cur.closed=True;self.candles.append(self._cur);closed=self._cur;self._cur=Candle(cts,price)
        else: self._cur.update(price,vol)
        return closed
    @property
    def count(self): return len(self.candles)
class BinanceFetcher:
    async def fetch_price(self,sym):
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r=await c.get(BINANCE_KLINES,params={"symbol":sym,"interval":"1m","limit":2})
                if r.status_code==200:
                    d=r.json()
                    if d: return {"price":float(d[-1][4]),"open":float(d[-1][1]),"high":float(d[-1][2]),"low":float(d[-1][3]),"close":float(d[-1][4]),"volume":float(d[-1][5])}
        except Exception as e: logger.debug(f"Binance {sym}: {e}")
        return None
    async def fetch_hist(self,sym,interval="15m",limit=100):
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r=await c.get(BINANCE_KLINES,params={"symbol":sym,"interval":interval,"limit":limit})
                if r.status_code==200:
                    d=r.json();o,h,l,cl,v=[],[],[],[],[]
                    for x in d: o.append(float(x[1]));h.append(float(x[2]));l.append(float(x[3]));cl.append(float(x[4]));v.append(float(x[5]))
                    if len(cl)>=20: return {"opens":o,"highs":h,"lows":l,"closes":cl,"volumes":v}
        except Exception as e: logger.debug(f"Binance hist {sym}: {e}")
        return None
class ForexFetcher:
    async def fetch_price(self,sym):
        base=sym[:3].upper();quote=sym[3:].upper()
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r=await c.get(f"https://open.er-api.com/v6/latest/{base}")
                if r.status_code==200:
                    p=r.json().get("rates",{}).get(quote)
                    if p: return {"price":float(p),"open":float(p),"high":float(p)*1.001,"low":float(p)*0.999,"close":float(p),"volume":1000.0}
        except Exception as e: logger.debug(f"er-api {sym}: {e}")
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r=await c.get(f"https://api.frankfurter.app/latest?from={base}&to={quote}")
                if r.status_code==200:
                    p=r.json().get("rates",{}).get(quote)
                    if p: return {"price":float(p),"open":float(p),"high":float(p)*1.001,"low":float(p)*0.999,"close":float(p),"volume":1000.0}
        except Exception as e: logger.debug(f"Forex {sym}: {e}")
        return None
    async def fetch_hist_yf(self,sym,tf):
        """Fetch per-timeframe historical bars from Yahoo Finance."""
        yf=YF_FOREX_SYM.get(sym.upper(),f"{sym}=X")
        iv=TF_YAHOO_INTERVAL.get(tf,"15m");rng=TF_YAHOO_RANGE.get(tf,"5d")
        url=f"https://query1.finance.yahoo.com/v8/finance/chart/{yf}"
        try:
            async with httpx.AsyncClient(timeout=15.0,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}) as c:
                r=await c.get(url,params={"interval":iv,"range":rng,"includePrePost":"false"})
                if r.status_code!=200: logger.warning(f"YF {yf} {tf}m: HTTP {r.status_code}"); return None
                res=r.json().get("chart",{}).get("result",[])
                if not res: return None
                q=res[0].get("indicators",{}).get("quote",[{}])[0]
                cls=[x or 0.0 for x in q.get("close",[])];ops=[x or 0.0 for x in q.get("open",[])]
                his=[x or 0.0 for x in q.get("high",[])];los=[x or 0.0 for x in q.get("low",[])]
                vls=[float(x) if x else 1000.0 for x in q.get("volume",[])]
                f=[(o,h,l,c,v) for o,h,l,c,v in zip(ops,his,los,cls,vls) if c and c>0]
                if len(f)<20: return None
                o2,h2,l2,c2,v2=zip(*f)
                return {"opens":list(o2),"highs":list(h2),"lows":list(l2),"closes":list(c2),"volumes":list(v2)}
        except Exception as e: logger.debug(f"YF hist {sym} {tf}m: {e}"); return None
class EveningSessionEngine:
    def __init__(self):
        self._bin=BinanceFetcher();self._forex=ForexFetcher();self._running=False
        self._b={i["symbol"]:{tf:CandleBuilder(i["symbol"],tf) for tf in TIMEFRAMES} for i in EVENING_INSTRUMENTS}
        self._prices={};self._cb=None;self._last_sigs={}
    def set_signal_callback(self,cb): self._cb=cb
    def is_active(self):
        now=datetime.now(IST)
        if now.weekday()==5: return False   # Saturday — Forex/Gold liquidity too thin
        hm=now.hour*60+now.minute
        return SESSION_START_H*60+SESSION_START_M<=hm<24*60
    def is_kz(self):
        hm=datetime.now(IST).hour*60+datetime.now(IST).minute
        return any((h1*60+m1)<=hm<=(h2*60+m2) for h1,m1,h2,m2 in KILLZONES)
    def mins_to_start(self):
        now=datetime.now(IST);hm=now.hour*60+now.minute;s=SESSION_START_H*60+SESSION_START_M
        return s-hm if hm<s else 24*60-hm+s
    async def _fetch_prices(self):
        tasks={i["symbol"]:(self._bin.fetch_price(i["symbol"]) if i["exchange"]=="BINANCE" else self._forex.fetch_price(i["symbol"])) for i in EVENING_INSTRUMENTS}
        fetched=await asyncio.gather(*tasks.values(),return_exceptions=True)
        return {sym:r for sym,r in zip(tasks.keys(),fetched) if isinstance(r,dict) and r}
    async def _process(self,sym,tf,inst_info):
        key=f"{sym}:{tf}"
        if key in self._last_sigs and (datetime.now(timezone.utc).replace(tzinfo=None)-self._last_sigs[key]).total_seconds()/60<60: return
        try:
            from mcp_server.tools.institutional_detector import analyze_institutional_activity
            from mcp_server.tools.decision_engine import score_decision
            from mcp_server.tools.claude_analyzer import evaluate_setup
            # Fetch bars live on-demand — no warmup period, no preload required
            if inst_info["exchange"]=="BINANCE":
                m=await self._bin.fetch_hist(sym,TF_BINANCE[tf],100)
            else:
                m=await self._forex.fetch_hist_yf(sym,tf)
            if not m or len(m.get("closes",[]))<20:
                logger.debug(f"No data for {inst_info['display']} {tf}m — skipping")
                return
            logger.info(f"Analyzing {inst_info['display']} {tf}m ({len(m['closes'])} bars)...")
            o=m["opens"];h=m["highs"];l=m["lows"];c=m["closes"];v=m["volumes"];cur=c[-1]
            from mcp_server.tools.institutional_detector import detect_htf_structure
            def _slice(arr,n):return arr[-n:] if len(arr)>=n else arr
            weekly=detect_htf_structure(_slice(c,100),_slice(h,100),_slice(l,100))
            daily=detect_htf_structure(_slice(c,50),_slice(h,50),_slice(l,50))
            h4=detect_htf_structure(_slice(c,20),_slice(h,20),_slice(l,20))
            _sl=min(l[-25:-4]) if len(l)>=25 else (min(l[:-3]) if len(l)>3 else min(l))
            _sh=max(h[-25:-4]) if len(h)>=25 else (max(h[:-3]) if len(h)>3 else max(h))
            inst=analyze_institutional_activity(o,h,l,c,v,_sl,_sh,weekly)
            if inst.institutional_bias=="NEUTRAL": return
            # Skip plain mitigated OBs — only trade them if there's compensating structure
            if inst.mitigation_block and not (inst.breaker_block or inst.propulsion_block):
                logger.debug(f"Skip {inst_info['display']} {tf}m — OB already mitigated, no breaker/propulsion")
                return
            # Suppress signals outside each instrument's high-liquidity session windows
            now_hm=datetime.now(IST);hm=now_hm.hour*60+now_hm.minute
            if not any(s<=hm<=e for s,e in INST_SESSIONS.get(sym,[(SESSION_START_H*60+SESSION_START_M,24*60)])):
                logger.debug(f"Skip {inst_info['display']} {tf}m — outside active session window ({hm//60:02d}:{hm%60:02d} IST)")
                return
            sd="LONG" if inst.institutional_bias=="BULLISH" else "SHORT"
            ind_conf=inst.inducement_confirmed;ind_lvl=inst.inducement_level
            trap=inst.liquidity_event.value in ("SSL_SWEPT","BSL_SWEPT","IND_BULL","IND_BEAR","TURTLE_BULL","TURTLE_BEAR")
            ltf=False
            if len(c)>=6:
                pt="UP" if c[-4]>c[-6] else "DOWN";ct="UP" if c[-1]>c[-3] else "DOWN";ltf=pt!=ct
            avg_v=sum(v[-20:])/20 if len(v)>=20 else v[-1];vs=v[-1]>avg_v*1.5
            kz=self.is_kz()
            eq=(max(h[-50:])+min(l[-50:]))/2 if len(h)>=50 else (max(h[-20:])+min(l[-20:]))/2
            poi="BREAKER" if inst.breaker_block else "OB_FVG" if inst.propulsion_block else "OB"
            # ── Pre-score: skip Claude for clearly weak setups ─────────────
            pre=score_decision(weekly_trend=weekly,daily_structure=daily,h4_flow=h4,signal_direction=sd,institutional=inst,poi_type=poi,trap_confirmed=trap,ltf_choch=ltf,volume_spike=vs,in_discount=cur<eq,is_index=False,is_killzone=kz,is_session_open=True,htf_ob_confluence=inst.breaker_block,first_touch_ob=not inst.mitigation_block,ob_already_touched=inst.mitigation_block,segment=inst_info["segment"],inducement_confirmed=ind_conf)
            if pre.score<50:
                logger.debug(f"Pre-score {pre.score:.0f}<50 for {inst_info['display']} {tf}m — skip Claude")
                return
            claude=await evaluate_setup(symbol=inst_info["display"],segment=inst_info["segment"],timeframe=tf,current_price=cur,closes=c,highs=h,lows=l,volumes=v,inst_bias=inst.institutional_bias,inst_score=inst.total_score,inst_evidence=inst.evidence,liquidity_event=inst.liquidity_event.value,breaker_block=inst.breaker_block,propulsion_block=inst.propulsion_block,mitigation_block=inst.mitigation_block,wyckoff_phase=inst.wyckoff_phase.value,weekly_trend=weekly,daily_structure=daily,h4_flow=h4,in_discount=cur<eq,is_killzone=kz,ltf_choch=ltf,volume_spike=vs,inducement_confirmed=ind_conf,inducement_level=ind_lvl)
            if claude is not None:
                if not claude.send:logger.info(f"Claude rejected {inst_info['display']} {tf}m: {claude.risk_factors}");return
                sd=claude.direction;sig_grade=claude.grade;sig_score=claude.confidence
                sig_narrative=claude.narrative+" | ".join(claude.key_reasons[:2]);sig_evidence=claude.key_reasons+inst.evidence[:2]
            else:
                if not pre.send: return
                sig_grade=pre.grade;sig_score=pre.score;sig_narrative=pre.narrative;sig_evidence=pre.evidence
            # Determine signal_type from institutional structure
            sig_type=("BREAKER" if inst.breaker_block else "OB_ENTRY" if inst.propulsion_block else "OB_ENTRY" if inst.ob_high else "INSTITUTIONAL")
            # OB-aware SL/TP: use actual OB zone when price is at or near the block
            pip=inst_info.get("pip",0.0001)
            atr=_calc_atr(h,l,c)
            prec=5 if pip<=0.0001 else 2
            ob_h=inst.ob_high;ob_l=inst.ob_low
            ob_m=round((ob_h+ob_l)/2,prec) if ob_h and ob_l else None
            if ob_h and ob_l and ob_l<cur<ob_h*1.02:
                # Price inside or just above the OB zone — SL beyond OB extreme
                raw_sl=ob_l*(1-0.0005) if sd=="LONG" else ob_h*(1+0.0005)
                sl_dist=max(abs(cur-raw_sl),atr*0.5)   # floor: half ATR
            else:
                sl_dist=max(atr*1.5,pip*15)             # pure ATR fallback
            entry=cur
            sl=round(cur-sl_dist if sd=="LONG" else cur+sl_dist,prec)
            tp1=round(cur+sl_dist if sd=="LONG" else cur-sl_dist,prec)
            tp2=round(cur+sl_dist*2 if sd=="LONG" else cur-sl_dist*2,prec)
            tp3=round(cur+sl_dist*3 if sd=="LONG" else cur-sl_dist*3,prec)
            sl_pts=abs(entry-sl)
            sess="LONDON" if 13*60+30<=hm<=17*60+30 else "NEW_YORK" if 18*60+30<=hm<=23*60 else "EVENING"
            charges=round(cur*0.0008,2);rr=round(sl_pts*2/sl_pts,1) if sl_pts>0 else 2.0
            sig={"instrument":f"{inst_info['exchange']}:{inst_info['display']}","base_symbol":inst_info["display"],"exchange":inst_info["exchange"],"segment":inst_info["segment"],"direction":sd,"timeframe":str(tf),"signal_type":sig_type,"score":sig_score,"grade":sig_grade,"entry":round(entry,prec),"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"sl_points":round(sl_pts,prec),"sl_percent":round(sl_pts/entry*100,3) if entry>0 else 0,"rr_ratio":rr,"lots":1,"charges":charges,"net_at_tp1":round(sl_pts-charges,2),"net_at_tp2":round(sl_pts*2-charges,2),"net_at_tp3":round(sl_pts*3-charges,2),"atr":round(atr,prec),"htf_bias":inst.institutional_bias,"htf_matches_direction":True,"h4_flow":h4,"in_discount":cur<eq,"liquidity_swept":inst.liquidity_event.value!="NONE","fvg_present":inst.propulsion_block,"volume_ratio":round(v[-1]/avg_v,2),"session":sess,"trap_present":trap,"is_killzone":kz,"ltf_choch":ltf,"ob_already_touched":inst.mitigation_block,"ob_high":ob_h,"ob_low":ob_l,"ob_mid":ob_m,"close":cur,"in_lunch":False,"inducement_confirmed":ind_conf,"inducement_level":ind_lvl,"options_pcr":None,"options_oi_bias":None,"max_pain":None,"setup_type":f"{inst.wyckoff_phase.value}|{inst.liquidity_event.value}","narrative":sig_narrative,"htf_timeframe":"4H","confluences":{"htf_bias":inst.institutional_bias,"poi_type":poi,"zone_type":"Discount" if cur<eq else "Premium","liquidity_swept":inst.liquidity_event.value!="NONE","killzone":kz,"ltf_choch":ltf,"inducement":ind_conf},"institutional_evidence":inst.evidence,"decision_evidence":sig_evidence,"options_signal":None}
            self._last_sigs[key]=datetime.now(timezone.utc).replace(tzinfo=None)
            logger.info(f"EVENING SIGNAL: {inst_info['display']} {sd} score={sig_score} grade={sig_grade} tf={tf}m sess={sess} type={sig_type} entry={round(entry,prec)} sl={sl} tp2={tp2} atr={round(atr,prec)} ob_h={ob_h} ob_l={ob_l}")
            if self._cb: await self._cb(sig)
        except Exception as e: logger.error(f"Evening process {sym} {tf}m: {e}",exc_info=True)
    async def _tick(self):
        prices=await self._fetch_prices()
        if not prices:
            logger.warning("Evening tick: no prices fetched")
            return
        now=datetime.now(IST)
        price_summary=", ".join(f"{inst['display']}={prices[inst['symbol']]['price']:.4f}" for inst in EVENING_INSTRUMENTS if inst["symbol"] in prices)
        logger.info(f"Evening tick @ {now.strftime('%H:%M:%S')} IST | {price_summary}")
        for inst in EVENING_INSTRUMENTS:
            sym=inst["symbol"];data=prices.get(sym)
            if not data or not data.get("price"): continue
            p=data["price"];vol=data.get("volume",1000.0);self._prices[sym]=p
            for tf in TIMEFRAMES:
                closed=self._b[sym][tf].update(p,now,vol)
                if closed:
                    await self._process(sym,tf,inst)
    async def run(self):
        self._running=True;logger.info("Evening engine started — bars fetched live on-demand")
        while self._running:
            try:
                if self.is_active():
                    await self._tick();await asyncio.sleep(60 if self.is_kz() else 120)
                else:
                    mins=self.mins_to_start();sl=1800 if mins>60 else 60
                    logger.info(f"Evening session starts in {mins}min.");await asyncio.sleep(sl)
            except asyncio.CancelledError: break
            except Exception as e: logger.error(f"Evening engine: {e}",exc_info=True);await asyncio.sleep(30)
        self._running=False
    def stop(self): self._running=False
    def get_prices(self): return dict(self._prices)

_eng=None
def get_evening_engine():
    global _eng
    if _eng is None: _eng=EveningSessionEngine()
    return _eng
