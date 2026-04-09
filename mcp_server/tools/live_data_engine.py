import asyncio,logging
from collections import deque
from datetime import datetime,date,timedelta
from typing import Optional,Dict,List
import httpx,pytz
logger=logging.getLogger(__name__)
IST=pytz.timezone("Asia/Kolkata")
INDICES=[
    {"symbol":"NIFTY","exchange":"NSE","segment":"INDICES","lot_size":50},
    {"symbol":"BANKNIFTY","exchange":"NSE","segment":"INDICES","lot_size":15},
    {"symbol":"FINNIFTY","exchange":"NSE","segment":"INDICES","lot_size":40},
    {"symbol":"MIDCPNIFTY","exchange":"NSE","segment":"INDICES","lot_size":75},
]
NSE_HOME="https://www.nseindia.com"
NSE_INDEX_URL="https://www.nseindia.com/api/allIndices"
NSE_HIST_URL="https://www.nseindia.com/api/historical/indicesHistory"
NSE_OC_INDEX="https://www.nseindia.com/api/option-chain-indices"
TIMEFRAMES=[1,5,15,60]

class Candle:
    __slots__=["ts","open","high","low","close","volume","closed"]
    def __init__(self,ts,o):
        self.ts=ts;self.open=o;self.high=o;self.low=o;self.close=o;self.volume=0.0;self.closed=False
    def update(self,p,v=0):
        self.high=max(self.high,p);self.low=min(self.low,p);self.close=p;self.volume+=v

class CandleBuilder:
    def __init__(self,sym,tf,mx=500):
        self.sym=sym;self.tf=tf;self.candles=deque(maxlen=mx);self._cur=None
    def _start(self,ts):
        tm=ts.hour*60+ts.minute;cs=(tm//self.tf)*self.tf
        return ts.replace(hour=cs//60,minute=cs%60,second=0,microsecond=0)
    def update(self,price,ts,vol=0):
        cts=self._start(ts);closed=None
        if self._cur is None:self._cur=Candle(cts,price)
        elif cts>self._cur.ts:
            self._cur.closed=True;self.candles.append(self._cur);closed=self._cur;self._cur=Candle(cts,price)
        else:self._cur.update(price,vol)
        return closed
    def get_ohlcv(self):
        c=list(self.candles)
        return {"opens":[x.open for x in c],"highs":[x.high for x in c],"lows":[x.low for x in c],"closes":[x.close for x in c],"volumes":[x.volume for x in c]}
    @property
    def count(self):return len(self.candles)

class NSEFetcher:
    def __init__(self):self._ck={};self._cts=None;self._ttl=1800
    async def _refresh(self):
        now=datetime.utcnow()
        if self._cts and (now-self._cts).total_seconds()<self._ttl and self._ck:return self._ck
        try:
            async with httpx.AsyncClient(timeout=10.0,headers={"User-Agent":"Mozilla/5.0"}) as c:
                r=await c.get(NSE_HOME);self._ck=dict(r.cookies);self._cts=now
        except Exception as e:logger.warning(f"Cookie: {e}")
        return self._ck
    def _h(self):return {"User-Agent":"Mozilla/5.0","Accept":"application/json","Referer":"https://www.nseindia.com/","X-Requested-With":"XMLHttpRequest"}
    async def fetch_indices(self):
        ck=await self._refresh();result={}
        sm={"NIFTY 50":"NIFTY","NIFTY BANK":"BANKNIFTY","NIFTY FIN SERVICE":"FINNIFTY","NIFTY MIDCAP SELECT":"MIDCPNIFTY","Nifty 50":"NIFTY","Nifty Bank":"BANKNIFTY"}
        try:
            async with httpx.AsyncClient(timeout=10.0,cookies=ck,headers=self._h()) as c:
                r=await c.get(NSE_INDEX_URL)
                if r.status_code==200:
                    for idx in r.json().get("data",[]):
                        nm=idx.get("indexSymbol",idx.get("index",""));mp=sm.get(nm)
                        if mp:result[mp]={"price":float(idx.get("last",idx.get("lastPrice",0))or 0),"volume":float(idx.get("totalTurnover",1000)or 1000)}
        except Exception as e:logger.warning(f"NSE: {e}")
        return result
    async def fetch_oc(self,sym):
        ck=await self._refresh()
        try:
            async with httpx.AsyncClient(timeout=15.0,cookies=ck,headers=self._h()) as c:
                r=await c.get(NSE_OC_INDEX,params={"symbol":sym})
                if r.status_code==200:return r.json()
        except Exception as e: logger.warning(f"OC fetch {sym}: {e}")
        return None

class LiveDataEngine:
    def __init__(self):
        self._f=NSEFetcher();self._running=False
        self._b={i["symbol"]:{tf:CandleBuilder(i["symbol"],tf) for tf in TIMEFRAMES} for i in INDICES}
        self._hist={};self._oc={};self._oc_ts={};self._oc_ttl=180
        self._prices={};self._cb=None

    def set_signal_callback(self,cb):self._cb=cb

    def is_open(self):
        now=datetime.now(IST);wd=now.weekday()
        if wd>=5:return False
        hm=now.hour*60+now.minute
        return 9*60+15<=hm<=15*60+30

    def mins_to_open(self):
        now=datetime.now(IST);hm=now.hour*60+now.minute;ohm=9*60+15;wd=now.weekday()
        if wd>=5:return (7-wd)*24*60
        return ohm-hm if hm<ohm else 24*60-hm+ohm

    async def _get_oc(self,sym):
        now=datetime.utcnow();ts=self._oc_ts.get(sym)
        if ts and (now-ts).total_seconds()<self._oc_ttl:return self._oc.get(sym)
        raw=await self._f.fetch_oc(sym)
        if raw:self._oc[sym]=raw;self._oc_ts[sym]=now
        return raw

    async def _preload_historical(self):
        logger.info("Preloading historical data from NSE...")
        for inst in INDICES:
            sym=inst["symbol"]
            try:
                ck=await self._f._refresh()
                end=date.today();start=end-timedelta(days=60)
                params={"indexType":sym,"from":start.strftime("%d-%m-%Y"),"to":end.strftime("%d-%m-%Y")}
                async with httpx.AsyncClient(timeout=15.0,cookies=ck,headers=self._f._h()) as c:
                    r=await c.get(NSE_HIST_URL,params=params)
                    if r.status_code==200:
                        rows=r.json().get("data",{}).get("indexCloseOnlineRecords",[])
                        if not self._hist.get(sym):self._hist[sym]={"opens":[],"highs":[],"lows":[],"closes":[],"volumes":[]}
                        for row in rows:
                            try:
                                o=float(row.get("EOD_OPEN_INDEX_VAL",0)or 0);h=float(row.get("EOD_HIGH_INDEX_VAL",0)or 0)
                                l=float(row.get("EOD_LOW_INDEX_VAL",0)or 0);cl=float(row.get("EOD_CLOSING_INDEX_VAL",0)or 0)
                                if cl>0:
                                    self._hist[sym]["opens"].append(o);self._hist[sym]["highs"].append(h)
                                    self._hist[sym]["lows"].append(l);self._hist[sym]["closes"].append(cl)
                                    self._hist[sym]["volumes"].append(1000.0)
                            except Exception: pass
                        logger.info(f"Preloaded {sym}: {len(self._hist.get(sym,{}).get('closes',[]))} candles")
            except Exception as e:logger.warning(f"Preload failed {sym}: {e}")
            await asyncio.sleep(0.5)

    async def _process(self,sym,tf,inst_info):
        try:
            from mcp_server.tools.institutional_detector import analyze_institutional_activity
            from mcp_server.tools.decision_engine import score_decision
            from mcp_server.tools.options_analysis import analyze_option_chain,is_near_max_pain
            ohlcv=self._b[sym][tf].get_ohlcv();hist=self._hist.get(sym,{})
            m={k:(hist.get(k,[])+ohlcv.get(k,[]))[-200:] for k in ("opens","highs","lows","closes","volumes")}
            if len(m["closes"])<30:return
            o=m["opens"];h=m["highs"];l=m["lows"];c=m["closes"];v=m["volumes"];cur=c[-1]
            from mcp_server.tools.institutional_detector import detect_htf_structure
            def _slice(arr,n):return arr[-n:] if len(arr)>=n else arr
            weekly=detect_htf_structure(_slice(c,100),_slice(h,100),_slice(l,100))
            daily=detect_htf_structure(_slice(c,50),_slice(h,50),_slice(l,50))
            h4=detect_htf_structure(_slice(c,20),_slice(h,20),_slice(l,20))
            inst=analyze_institutional_activity(o,h,l,c,v,min(l[-20:]),max(h[-20:]),weekly)
            if inst.institutional_bias=="NEUTRAL" and inst.total_score<10:return
            sd="LONG" if inst.institutional_bias=="BULLISH" else "SHORT"
            od={};raw_o=await self._get_oc(sym)
            if raw_o:od=analyze_option_chain(raw_o,cur)
            pcr=od.get("pcr",1.0);mp=od.get("max_pain");dir_o=od.get("options_direction","NEUTRAL")
            trap=inst.liquidity_event.value in ("SSL_SWEPT","BSL_SWEPT","IND_BULL","IND_BEAR","TURTLE_BULL","TURTLE_BEAR")
            ltf=False
            if len(c)>=6:
                pt="UP" if c[-4]>c[-6] else "DOWN";ct="UP" if c[-1]>c[-3] else "DOWN";ltf=pt!=ct
            avg_v=sum(v[-20:])/20 if len(v)>=20 else v[-1];vs=v[-1]>avg_v*1.5
            now_ist=datetime.now(IST);hm=now_ist.hour*60+now_ist.minute
            kz=any(abs(hm-(hh*60+mm))<=30 for hh,mm in [(9,15),(11,0),(13,30)])
            lunch=12*60<=hm<=13*60+30
            eq=(max(h[-50:])+min(l[-50:]))/2 if len(h)>=50 else (max(h[-20:])+min(l[-20:]))/2
            poi="BREAKER" if inst.breaker_block else "OB_FVG" if inst.propulsion_block else "OB"
            dec=score_decision(weekly_trend=weekly,daily_structure=daily,h4_flow=h4,signal_direction=sd,institutional=inst,poi_type=poi,trap_confirmed=trap,ltf_choch=ltf,volume_spike=vs,in_discount=cur<eq,pcr_confirms=(sd=="LONG" and dir_o=="BULLISH") or (sd=="SHORT" and dir_o=="BEARISH"),near_max_pain=mp and is_near_max_pain(cur,mp),gex_supports=od.get("gex",0)>0 if sd=="LONG" else od.get("gex",0)<0,options_conflict=(sd=="LONG" and dir_o=="BEARISH") or (sd=="SHORT" and dir_o=="BULLISH"),is_index=True,is_killzone=kz,is_session_open=kz,htf_ob_confluence=inst.breaker_block,first_touch_ob=not inst.mitigation_block,ob_already_touched=inst.mitigation_block,is_lunch_hour=lunch,low_volume_session=not kz and hm>15*60,segment="INDICES")
            if not dec.send:return
            os=None
            if od:
                from mcp_server.tools.options_strategy import select_strategy
                try:
                    dte=max(1,(3-date.today().weekday())%7 or 7)
                    os=select_strategy(underlying_price=cur,max_pain=mp or cur,pcr=pcr or 1.0,ce_walls=od.get("ce_walls",[]),pe_walls=od.get("pe_walls",[]),days_to_expiry=dte,instrument=sym,directional_bias=inst.institutional_bias)
                except Exception as e: logger.debug(f"Options strategy {sym}: {e}")
            sp=cur*0.004;entry=cur;sl=cur-sp if sd=="LONG" else cur+sp
            tp1=cur+sp if sd=="LONG" else cur-sp;tp2=cur+sp*2 if sd=="LONG" else cur-sp*2;tp3=cur+sp*3 if sd=="LONG" else cur-sp*3
            sl_pts=abs(entry-sl);lv=inst_info["lot_size"]
            sig={"instrument":f"NSE:{sym}","base_symbol":sym,"exchange":"NSE","segment":"INDICES","direction":sd,"timeframe":str(tf),"signal_type":"INSTITUTIONAL","score":dec.score,"grade":dec.grade,"entry":round(entry,2),"sl":round(sl,2),"tp1":round(tp1,2),"tp2":round(tp2,2),"tp3":round(tp3,2),"sl_points":round(sl_pts,2),"sl_percent":round(sl_pts/entry*100,3),"lots":1,"lot_size":lv,"charges":850.0,"net_at_tp1":round(sl_pts*lv-850,2),"htf_bias":inst.institutional_bias,"in_discount":cur<eq,"liquidity_swept":inst.liquidity_event.value!="NONE","fvg_present":inst.propulsion_block,"volume_ratio":round(v[-1]/avg_v,2),"session":"INDIA","trap_present":trap,"is_killzone":kz,"ltf_choch":ltf,"options_pcr":pcr,"options_oi_bias":dir_o,"max_pain":mp,"gex":od.get("gex"),"setup_type":f"{inst.wyckoff_phase.value}|{inst.liquidity_event.value}","narrative":dec.narrative,"htf_timeframe":"4H","confluences":{"htf_bias":inst.institutional_bias,"poi_type":poi,"zone_type":"Discount" if cur<eq else "Premium","liquidity_swept":inst.liquidity_event.value!="NONE","killzone":kz,"ltf_choch":ltf},"institutional_evidence":inst.evidence,"decision_evidence":dec.evidence,"options_signal":os}
            logger.info(f"SIGNAL: NSE:{sym} {sd} score={dec.score} grade={dec.grade} tf={tf}m")
            if self._cb:await self._cb(sig)
        except Exception as e:logger.error(f"Process {sym} {tf}m: {e}",exc_info=True)

    async def _tick(self):
        prices=await self._f.fetch_indices()
        if not prices:return
        now=datetime.now(IST)
        for inst in INDICES:
            sym=inst["symbol"];data=prices.get(sym)
            if not data or not data.get("price"):continue
            p=data["price"];vol=data.get("volume",1000);self._prices[sym]=p
            for tf in TIMEFRAMES:
                closed=self._b[sym][tf].update(p,now,vol)
                if closed and self._b[sym][tf].count>=30:
                    await self._process(sym,tf,inst)

    async def run(self):
        self._running=True
        logger.info("Live engine started")
        await self._preload_historical()
        while self._running:
            try:
                if self.is_open():
                    await self._tick()
                    await asyncio.sleep(60)
                else:
                    mins=self.mins_to_open()
                    sl=1800 if mins>60 else 60
                    logger.info(f"Market closed. Opens in {mins}min.")
                    await asyncio.sleep(sl)
            except asyncio.CancelledError:break
            except Exception as e:
                logger.error(f"Engine error: {e}",exc_info=True)
                await asyncio.sleep(30)
        self._running=False

    def stop(self):self._running=False
    def get_prices(self):return dict(self._prices)

_engine=None
def get_live_engine():
    global _engine
    if _engine is None:_engine=LiveDataEngine()
    return _engine
