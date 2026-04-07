import asyncio,logging
from collections import deque
from datetime import datetime,date
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
TIMEFRAMES=[1,5,15,60]
BINANCE_KLINES="https://api.binance.com/api/v3/klines"
KILLZONES=[(13,30,14,0),(18,30,19,0),(20,0,20,30)]
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
    def get_ohlcv(self):
        c=list(self.candles)
        return {"opens":[x.open for x in c],"highs":[x.high for x in c],"lows":[x.low for x in c],"closes":[x.close for x in c],"volumes":[x.volume for x in c]}
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
    async def fetch_hist(self,sym,interval="15m",limit=200):
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
        except: pass
        try:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r=await c.get(f"https://api.frankfurter.app/latest?from={base}&to={quote}")
                if r.status_code==200:
                    p=r.json().get("rates",{}).get(quote)
                    if p: return {"price":float(p),"open":float(p),"high":float(p)*1.001,"low":float(p)*0.999,"close":float(p),"volume":1000.0}
        except Exception as e: logger.debug(f"Forex {sym}: {e}")
        return None
class EveningSessionEngine:
    def __init__(self):
        self._bin=BinanceFetcher();self._forex=ForexFetcher();self._running=False
        self._b={i["symbol"]:{tf:CandleBuilder(i["symbol"],tf) for tf in TIMEFRAMES} for i in EVENING_INSTRUMENTS}
        self._hist={};self._prices={};self._cb=None;self._last_sigs={}
    def set_signal_callback(self,cb): self._cb=cb
    def is_active(self):
        now=datetime.now(IST);hm=now.hour*60+now.minute
        return SESSION_START_H*60+SESSION_START_M<=hm<24*60
    def is_kz(self):
        hm=datetime.now(IST).hour*60+datetime.now(IST).minute
        return any((h1*60+m1)<=hm<=(h2*60+m2) for h1,m1,h2,m2 in KILLZONES)
    def mins_to_start(self):
        now=datetime.now(IST);hm=now.hour*60+now.minute;s=SESSION_START_H*60+SESSION_START_M
        return s-hm if hm<s else 24*60-hm+s
    async def _load_hist(self):
        for inst in EVENING_INSTRUMENTS:
            sym=inst["symbol"]
            if inst["exchange"]=="BINANCE":
                h=await self._bin.fetch_hist(sym,"15m",200)
                if h: self._hist[sym]=h;logger.info(f"Hist loaded: {sym}")
            await asyncio.sleep(0.3)
    async def _fetch_prices(self):
        tasks={i["symbol"]:(self._bin.fetch_price(i["symbol"]) if i["exchange"]=="BINANCE" else self._forex.fetch_price(i["symbol"])) for i in EVENING_INSTRUMENTS}
        fetched=await asyncio.gather(*tasks.values(),return_exceptions=True)
        return {sym:r for sym,r in zip(tasks.keys(),fetched) if isinstance(r,dict) and r}
    async def _process(self,sym,tf,inst_info):
        key=f"{sym}:{tf}"
        if key in self._last_sigs and (datetime.utcnow()-self._last_sigs[key]).total_seconds()/60<60: return
        try:
            from mcp_server.tools.institutional_detector import analyze_institutional_activity
            from mcp_server.tools.decision_engine import score_decision
            ohlcv=self._b[sym][tf].get_ohlcv();hist=self._hist.get(sym,{})
            m={k:(hist.get(k,[])+ohlcv.get(k,[]))[-200:] for k in ("opens","highs","lows","closes","volumes")}
            if len(m["closes"])<30: return
            o=m["opens"];h=m["highs"];l=m["lows"];c=m["closes"];v=m["volumes"];cur=c[-1]
            def htf(cl):
                if len(cl)<20: return "NEUTRAL"
                mid=len(cl)//2;hh=max(cl[mid:])>max(cl[:mid]);hl=min(cl[mid:])>min(cl[:mid]);lh=max(cl[mid:])<max(cl[:mid]);ll=min(cl[mid:])<min(cl[:mid])
                return "BULLISH" if hh and hl else "BEARISH" if lh and ll else "NEUTRAL"
            weekly=htf(c[-100:] if len(c)>=100 else c);daily=htf(c[-50:] if len(c)>=50 else c);h4=htf(c[-20:] if len(c)>=20 else c)
            inst=analyze_institutional_activity(o,h,l,c,v,min(l[-20:]),max(h[-20:]),weekly)
            if inst.institutional_bias=="NEUTRAL" and inst.total_score<10: return
            sd="LONG" if inst.institutional_bias=="BULLISH" else "SHORT"
            trap=inst.liquidity_event.value in ("SSL_SWEPT","BSL_SWEPT","IND_BULL","IND_BEAR","TURTLE_BULL","TURTLE_BEAR")
            ltf=False
            if len(c)>=6:
                pt="UP" if c[-4]>c[-6] else "DOWN";ct="UP" if c[-1]>c[-3] else "DOWN";ltf=pt!=ct
            avg_v=sum(v[-20:])/20 if len(v)>=20 else v[-1];vs=v[-1]>avg_v*1.5
            kz=self.is_kz();now_ist=datetime.now(IST);hm=now_ist.hour*60+now_ist.minute
            eq=(max(h[-50:])+min(l[-50:]))/2 if len(h)>=50 else (max(h[-20:])+min(l[-20:]))/2
            poi="BREAKER" if inst.breaker_block else "OB_FVG" if inst.propulsion_block else "OB"
            dec=score_decision(weekly_trend=weekly,daily_structure=daily,h4_flow=h4,signal_direction=sd,institutional=inst,poi_type=poi,trap_confirmed=trap,ltf_choch=ltf,volume_spike=vs,in_discount=cur<eq,is_index=False,is_killzone=kz,is_session_open=kz,htf_ob_confluence=inst.breaker_block,first_touch_ob=not inst.mitigation_block,ob_already_touched=inst.mitigation_block,segment=inst_info["segment"])
            if not dec.send: return
            pip=inst_info.get("pip",0.0001);sp=pip*200
            entry=cur;sl=cur-sp if sd=="LONG" else cur+sp
            tp1=cur+sp if sd=="LONG" else cur-sp;tp2=cur+sp*2 if sd=="LONG" else cur-sp*2;tp3=cur+sp*3 if sd=="LONG" else cur-sp*3
            sl_pts=abs(entry-sl)
            sess="LONDON" if 13*60+30<=hm<=17*60+30 else "NEW_YORK" if 18*60+30<=hm<=23*60 else "EVENING"
            sig={"instrument":f"{inst_info['exchange']}:{inst_info['display']}","base_symbol":inst_info["display"],"exchange":inst_info["exchange"],"segment":inst_info["segment"],"direction":sd,"timeframe":str(tf),"signal_type":"INSTITUTIONAL","score":dec.score,"grade":dec.grade,"entry":round(entry,5),"sl":round(sl,5),"tp1":round(tp1,5),"tp2":round(tp2,5),"tp3":round(tp3,5),"sl_points":round(sl_pts,5),"sl_percent":round(sl_pts/entry*100,3) if entry>0 else 0,"lots":1,"charges":round(cur*0.0008,2),"net_at_tp1":round(sl_pts-cur*0.0008,2),"htf_bias":inst.institutional_bias,"in_discount":cur<eq,"liquidity_swept":inst.liquidity_event.value!="NONE","fvg_present":inst.propulsion_block,"volume_ratio":round(v[-1]/avg_v,2),"session":sess,"trap_present":trap,"is_killzone":kz,"ltf_choch":ltf,"options_pcr":None,"options_oi_bias":None,"max_pain":None,"setup_type":f"{inst.wyckoff_phase.value}|{inst.liquidity_event.value}","narrative":dec.narrative,"htf_timeframe":"4H","confluences":{"htf_bias":inst.institutional_bias,"poi_type":poi,"zone_type":"Discount" if cur<eq else "Premium","liquidity_swept":inst.liquidity_event.value!="NONE","killzone":kz,"ltf_choch":ltf},"institutional_evidence":inst.evidence,"decision_evidence":dec.evidence,"options_signal":None}
            self._last_sigs[key]=datetime.utcnow()
            logger.info(f"EVENING SIGNAL: {inst_info['display']} {sd} score={dec.score} grade={dec.grade} tf={tf}m sess={sess}")
            if self._cb: await self._cb(sig)
        except Exception as e: logger.error(f"Evening process {sym} {tf}m: {e}",exc_info=True)
    async def _tick(self):
        prices=await self._fetch_prices()
        if not prices: return
        now=datetime.now(IST)
        for inst in EVENING_INSTRUMENTS:
            sym=inst["symbol"];data=prices.get(sym)
            if not data or not data.get("price"): continue
            p=data["price"];vol=data.get("volume",1000.0);self._prices[sym]=p
            for tf in TIMEFRAMES:
                closed=self._b[sym][tf].update(p,now,vol)
                if closed and self._b[sym][tf].count>=30:
                    await self._process(sym,tf,inst)
    async def run(self):
        self._running=True;logger.info("Evening engine started")
        try: await self._load_hist()
        except Exception as e: logger.error(f"Hist load: {e}")
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
