import asyncio,logging
from collections import deque
from datetime import datetime,date,timedelta,timezone
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
# Only 15m and 60m — pass the INTRADAY timeframe filter in signal_filter.py
TIMEFRAMES=[15,60]
# Yahoo Finance symbols and per-TF settings for NSE index preload
YF_NSE_SYM={"NIFTY":"%5ENSEI","BANKNIFTY":"%5ENSEBANK","FINNIFTY":"%5ENSEFIN","MIDCPNIFTY":"%5ENSEMIDCAP"}
TF_YF_INTERVAL={1:"1m",5:"5m",15:"15m",60:"60m"}
TF_YF_RANGE={1:"1d",5:"5d",15:"5d",60:"30d"}
def _calc_atr(highs,lows,closes,period=14):
    """Average True Range — volatility measure for institutional SL sizing."""
    if len(closes)<2: return closes[-1]*0.002 if closes else 0.0
    trs=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]
    n=min(period,len(trs))
    return sum(trs[-n:])/n if n>0 else closes[-1]*0.002

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
        now=datetime.now(timezone.utc).replace(tzinfo=None)
        if self._cts and (now-self._cts).total_seconds()<self._ttl and self._ck:return self._ck
        try:
            async with httpx.AsyncClient(timeout=20.0,follow_redirects=True,headers=self._h()) as c:
                r=await c.get(NSE_HOME);ck=dict(r.cookies)
                await asyncio.sleep(1.5)
                r2=await c.get(f"{NSE_HOME}/option-chain");ck.update(dict(r2.cookies))
                await asyncio.sleep(1.0)
                r3=await c.get(f"{NSE_HOME}/market-data/live-equity-market");ck.update(dict(r3.cookies))
                if ck:self._ck=ck;self._cts=now;logger.info(f"NSE cookies refreshed: {list(ck.keys())}")
                else:logger.warning("NSE: no cookies returned from warm-up")
        except Exception as e:logger.warning(f"Cookie refresh: {e}")
        return self._ck
    def _h(self):return {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36","Accept":"application/json, text/plain, */*","Accept-Language":"en-US,en;q=0.9","Accept-Encoding":"gzip, deflate","Referer":"https://www.nseindia.com/option-chain","Connection":"keep-alive","Cache-Control":"no-cache","sec-ch-ua":'"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',"sec-ch-ua-mobile":"?0","sec-ch-ua-platform":'"Windows"',"sec-fetch-dest":"empty","sec-fetch-mode":"cors","sec-fetch-site":"same-origin","X-Requested-With":"XMLHttpRequest"}
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
        """Fetch option chain: NSE primary, Tickertape fallback."""
        ck=await self._refresh()
        try:
            async with httpx.AsyncClient(timeout=15.0,cookies=ck,headers=self._h()) as c:
                r=await c.get(NSE_OC_INDEX,params={"symbol":sym})
                if r.status_code==200:
                    logger.debug(f"OC {sym}: NSE OK")
                    return r.json()
                logger.warning(f"OC {sym}: NSE HTTP {r.status_code} — trying Tickertape")
        except Exception as e:
            logger.warning(f"OC {sym} NSE error: {e} — trying Tickertape")
        # ── Tickertape fallback ────────────────────────────────────
        tt_sym={"NIFTY":"NIFTY50","BANKNIFTY":"BANKNIFTY","FINNIFTY":"FINNIFTY","MIDCPNIFTY":"MIDCPNIFTY"}.get(sym,sym)
        try:
            async with httpx.AsyncClient(timeout=12.0,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json","Referer":"https://www.tickertape.in/"}) as c:
                r=await c.get(f"https://api.tickertape.in/options/chain",params={"sid":tt_sym})
                if r.status_code==200:
                    raw=r.json()
                    adapted=self._adapt_tickertape(raw,sym)
                    if adapted:
                        logger.info(f"OC {sym}: Tickertape OK ({len(adapted.get('records',{}).get('data',[]))} strikes)")
                        return adapted
        except Exception as e:
            logger.warning(f"OC {sym} Tickertape error: {e}")
        return None

    @staticmethod
    def _adapt_tickertape(raw:dict, sym:str) -> dict:
        """Convert Tickertape option chain response to NSE-compatible format."""
        try:
            chain=raw.get("data",{}).get("optionChain",[])
            if not chain: return {}
            records=[]
            underlying=raw.get("data",{}).get("underlyingValue",0) or 0
            for item in chain:
                strike=item.get("strike",0)
                ce=item.get("CE") or {}
                pe=item.get("PE") or {}
                records.append({"strikePrice":strike,
                    "CE":{"openInterest":ce.get("oi",0),"changeinOpenInterest":ce.get("coiC",0),"lastPrice":ce.get("ltp",0),"impliedVolatility":ce.get("iv",0)},
                    "PE":{"openInterest":pe.get("oi",0),"changeinOpenInterest":pe.get("coiC",0),"lastPrice":pe.get("ltp",0),"impliedVolatility":pe.get("iv",0)}})
            return {"records":{"data":records,"underlyingValue":underlying}}
        except Exception as e:
            logger.debug(f"Tickertape adapt {sym}: {e}")
            return {}

class LiveDataEngine:
    def __init__(self):
        self._f=NSEFetcher();self._running=False
        self._b={i["symbol"]:{tf:CandleBuilder(i["symbol"],tf) for tf in TIMEFRAMES} for i in INDICES}
        self._oc={};self._oc_ts={};self._oc_ttl=180
        self._prices={};self._cb=None;self._last_sigs={}

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
        now=datetime.now(timezone.utc).replace(tzinfo=None);ts=self._oc_ts.get(sym)
        if ts and (now-ts).total_seconds()<self._oc_ttl:return self._oc.get(sym)
        raw=await self._f.fetch_oc(sym)
        if raw:self._oc[sym]=raw;self._oc_ts[sym]=now
        return raw

    async def _fetch_hist_yf(self,sym,tf):
        """Fetch per-TF intraday bars from Yahoo Finance for NSE indices."""
        yf_sym=YF_NSE_SYM.get(sym,f"{sym}.NS")
        iv=TF_YF_INTERVAL.get(tf,"15m");rng=TF_YF_RANGE.get(tf,"5d")
        url=f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_sym}"
        try:
            async with httpx.AsyncClient(timeout=15.0,headers={"User-Agent":"Mozilla/5.0","Accept":"application/json"}) as c:
                r=await c.get(url,params={"interval":iv,"range":rng,"includePrePost":"false"})
                if r.status_code!=200: logger.warning(f"YF {yf_sym} {tf}m: HTTP {r.status_code}"); return None
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

    async def _process(self,sym,tf,inst_info):
        key=f"{sym}:{tf}"
        if key in self._last_sigs and (datetime.now(timezone.utc).replace(tzinfo=None)-self._last_sigs[key]).total_seconds()<3600:return
        try:
            from mcp_server.tools.institutional_detector import analyze_institutional_activity
            from mcp_server.tools.decision_engine import score_decision
            from mcp_server.tools.options_analysis import analyze_option_chain,is_near_max_pain
            from mcp_server.tools.claude_analyzer import evaluate_setup
            # Fetch bars live on-demand — no warmup period, no preload required
            m=await self._fetch_hist_yf(sym,tf)
            if not m or len(m.get("closes",[]))<20:
                logger.debug(f"No data for {sym} {tf}m — skipping")
                return
            logger.info(f"Analyzing {sym} {tf}m ({len(m['closes'])} bars)...")
            o=m["opens"];h=m["highs"];l=m["lows"];c=m["closes"];v=m["volumes"];cur=c[-1]
            from mcp_server.tools.institutional_detector import detect_htf_structure
            def _slice(arr,n):return arr[-n:] if len(arr)>=n else arr
            weekly=detect_htf_structure(_slice(c,100),_slice(h,100),_slice(l,100))
            daily=detect_htf_structure(_slice(c,50),_slice(h,50),_slice(l,50))
            h4=detect_htf_structure(_slice(c,20),_slice(h,20),_slice(l,20))
            # Use bars 4-25 ago as swing reference so recent sweeps can be detected
            _sl=min(l[-25:-4]) if len(l)>=25 else (min(l[:-3]) if len(l)>3 else min(l))
            _sh=max(h[-25:-4]) if len(h)>=25 else (max(h[:-3]) if len(h)>3 else max(h))
            inst=analyze_institutional_activity(o,h,l,c,v,_sl,_sh,weekly)
            if inst.institutional_bias=="NEUTRAL":return
            # Skip plain mitigated OBs — only trade with compensating structure
            if inst.mitigation_block and not (inst.breaker_block or inst.propulsion_block):
                logger.debug(f"Skip {sym} {tf}m — OB already mitigated, no breaker/propulsion")
                return
            sd="LONG" if inst.institutional_bias=="BULLISH" else "SHORT"
            lv=inst_info["lot_size"]
            od={};raw_o=await self._get_oc(sym)
            if raw_o:od=analyze_option_chain(raw_o,cur,lot_size=lv)
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
            # ── Claude-powered decision ────────────────────────────────
            claude=await evaluate_setup(symbol=sym,segment="INDICES",timeframe=tf,current_price=cur,closes=c,highs=h,lows=l,volumes=v,inst_bias=inst.institutional_bias,inst_score=inst.total_score,inst_evidence=inst.evidence,liquidity_event=inst.liquidity_event.value,breaker_block=inst.breaker_block,propulsion_block=inst.propulsion_block,mitigation_block=inst.mitigation_block,wyckoff_phase=inst.wyckoff_phase.value,weekly_trend=weekly,daily_structure=daily,h4_flow=h4,in_discount=cur<eq,is_killzone=kz,ltf_choch=ltf,volume_spike=vs,options_data=od or None)
            if claude is not None:
                if not claude.send:logger.info(f"Claude rejected {sym} {tf}m: {claude.risk_factors}");return
                sd=claude.direction;sig_grade=claude.grade;sig_score=claude.confidence
                sig_narrative=claude.narrative+" | ".join(claude.key_reasons[:2])
                sig_evidence=claude.key_reasons+inst.evidence[:2]
            else:
                # Fallback to scoring engine
                dec=score_decision(weekly_trend=weekly,daily_structure=daily,h4_flow=h4,signal_direction=sd,institutional=inst,poi_type=poi,trap_confirmed=trap,ltf_choch=ltf,volume_spike=vs,in_discount=cur<eq,pcr_confirms=(sd=="LONG" and dir_o=="BULLISH") or (sd=="SHORT" and dir_o=="BEARISH"),near_max_pain=mp and is_near_max_pain(cur,mp),gex_supports=od.get("gex",0)>0 if sd=="LONG" else od.get("gex",0)<0,options_conflict=(sd=="LONG" and dir_o=="BEARISH") or (sd=="SHORT" and dir_o=="BULLISH"),is_index=True,is_killzone=kz,is_session_open=(not lunch),htf_ob_confluence=inst.breaker_block,first_touch_ob=not inst.mitigation_block,ob_already_touched=inst.mitigation_block,is_lunch_hour=lunch,low_volume_session=not kz and hm>15*60,segment="INDICES")
                if not dec.send:return
                sig_grade=dec.grade;sig_score=dec.score;sig_narrative=dec.narrative;sig_evidence=dec.evidence
            os=None
            if od:
                from mcp_server.tools.options_strategy import select_strategy
                try:
                    dte=max(1,(3-date.today().weekday())%7 or 7)
                    os=select_strategy(underlying_price=cur,max_pain=mp or cur,pcr=pcr or 1.0,ce_walls=od.get("ce_walls",[]),pe_walls=od.get("pe_walls",[]),days_to_expiry=dte,instrument=sym,directional_bias=inst.institutional_bias)
                except Exception as e: logger.debug(f"Options strategy {sym}: {e}")
            # Determine signal_type from institutional structure
            sig_type=("BREAKER" if inst.breaker_block else "OB_ENTRY" if inst.propulsion_block else "OB_ENTRY" if inst.ob_high else "INSTITUTIONAL")
            # OB-aware SL/TP — use actual OB zone when price is near the block
            ob_h=inst.ob_high;ob_l=inst.ob_low
            ob_m=round((ob_h+ob_l)/2,2) if ob_h and ob_l else None
            atr=_calc_atr(h,l,c)
            if ob_h and ob_l and ob_l<cur<ob_h*1.02:
                raw_sl=ob_l*(1-0.0005) if sd=="LONG" else ob_h*(1+0.0005)
                sl_dist=max(abs(cur-raw_sl),atr*0.5,cur*0.001)   # floor: half ATR or 0.1%
            else:
                sl_dist=max(atr*1.5,cur*0.002)                   # pure ATR fallback, floor 0.2%
            entry=cur
            sl=round(cur-sl_dist if sd=="LONG" else cur+sl_dist, 2)
            tp1=round(cur+sl_dist if sd=="LONG" else cur-sl_dist, 2)
            tp2=round(cur+sl_dist*2 if sd=="LONG" else cur-sl_dist*2, 2)
            tp3=round(cur+sl_dist*3 if sd=="LONG" else cur-sl_dist*3, 2)
            sl_pts=abs(entry-sl)
            # Risk-based position sizing: floor 1 lot, cap 10 lots
            from mcp_server.config import get_settings as _gs
            _cfg=_gs();_risk=_cfg.account_size*_cfg.risk_per_trade_percent/100
            lots=max(1,min(10,int(_risk/(sl_pts*lv)))) if sl_pts>0 and lv>0 else 1
            charges=round(sl_pts*lv*lots*0.0003+850, 2)  # approx brokerage+STT+GST
            sig={"instrument":f"NSE:{sym}","base_symbol":sym,"exchange":"NSE","segment":"INDICES","direction":sd,"timeframe":str(tf),"signal_type":sig_type,"score":sig_score,"grade":sig_grade,"entry":round(entry,2),"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"sl_points":round(sl_pts,2),"sl_percent":round(sl_pts/entry*100,3),"rr_ratio":2.0,"lots":lots,"lot_size":lv,"charges":charges,"net_at_tp1":round(sl_pts*lv*lots-charges,2),"net_at_tp2":round(sl_pts*lv*lots*2-charges,2),"net_at_tp3":round(sl_pts*lv*lots*3-charges,2),"atr":round(atr,2),"htf_bias":inst.institutional_bias,"htf_matches_direction":True,"in_discount":cur<eq,"liquidity_swept":inst.liquidity_event.value!="NONE","fvg_present":inst.propulsion_block,"volume_ratio":round(v[-1]/avg_v,2),"session":"INDIA","trap_present":trap,"is_killzone":kz,"ltf_choch":ltf,"ob_already_touched":inst.mitigation_block,"ob_high":ob_h,"ob_low":ob_l,"ob_mid":ob_m,"close":cur,"in_lunch":lunch,"options_pcr":pcr,"options_oi_bias":dir_o,"max_pain":mp,"gex":od.get("gex"),"setup_type":f"{inst.wyckoff_phase.value}|{inst.liquidity_event.value}","narrative":sig_narrative,"htf_timeframe":"4H","confluences":{"htf_bias":inst.institutional_bias,"poi_type":poi,"zone_type":"Discount" if cur<eq else "Premium","liquidity_swept":inst.liquidity_event.value!="NONE","killzone":kz,"ltf_choch":ltf},"institutional_evidence":inst.evidence,"decision_evidence":sig_evidence,"options_signal":os}
            self._last_sigs[key]=datetime.now(timezone.utc).replace(tzinfo=None)
            logger.info(f"SIGNAL: NSE:{sym} {sd} score={sig_score:.0f} grade={sig_grade} tf={tf}m type={sig_type} entry={round(entry,2)} sl={sl} tp2={tp2} lots={lots} atr={round(atr,2)} ob_h={ob_h} ob_l={ob_l} (claude={'yes' if claude else 'fallback'})")
            if self._cb:await self._cb(sig)
        except Exception as e:logger.error(f"Process {sym} {tf}m: {e}",exc_info=True)

    async def _tick(self):
        prices=await self._f.fetch_indices()
        if not prices:
            logger.warning("Live tick: no data from NSE")
            return
        now=datetime.now(IST)
        price_summary=", ".join(f"{sym}={prices[sym]['price']:.0f}" for sym in ("NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY") if sym in prices)
        candle_counts={sym:{tf:self._b[sym][tf].count for tf in TIMEFRAMES} for inst in INDICES for sym in [inst["symbol"]]}
        logger.info(f"Live tick @ {now.strftime('%H:%M:%S')} IST | {price_summary} | candles={candle_counts}")
        for inst in INDICES:
            sym=inst["symbol"];data=prices.get(sym)
            if not data or not data.get("price"):continue
            p=data["price"];vol=data.get("volume",1000);self._prices[sym]=p
            for tf in TIMEFRAMES:
                closed=self._b[sym][tf].update(p,now,vol)
                if closed:
                    await self._process(sym,tf,inst)

    async def run(self):
        self._running=True
        logger.info("Live engine started — bars fetched live on-demand")
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
