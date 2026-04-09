import math, logging
from dataclasses import dataclass, field
from typing import Optional, List
from enum import Enum
logger = logging.getLogger(__name__)

class OptionsStrategy(Enum):
    IRON_CONDOR="Iron Condor"; SHORT_STRADDLE="Short Straddle"
    SHORT_STRANGLE="Short Strangle"; IRON_BUTTERFLY="Iron Butterfly"
    JADE_LIZARD="Jade Lizard"; REVERSE_JADE_LIZARD="Reverse Jade Lizard"
    NONE="None"

@dataclass
class OptionsSignal:
    strategy: OptionsStrategy
    instrument: str
    sell_call_strike: Optional[float]=None; sell_put_strike: Optional[float]=None
    buy_call_strike: Optional[float]=None; buy_put_strike: Optional[float]=None
    sell_call_premium: float=0.0; sell_put_premium: float=0.0
    total_credit: float=0.0; max_profit: float=0.0; max_loss: float=0.0
    breakeven_upper: float=0.0; breakeven_lower: float=0.0; stop_loss: float=0.0
    iv_rank: float=0.0; pcr: float=0.0; max_pain: float=0.0
    underlying_price: float=0.0; days_to_expiry: int=7
    reason: str=""; confidence: str="MEDIUM"; valid: bool=False

def _strike(price, step=50):
    return round(price/step)*step

def _premium(S, K, dte, iv=0.15, call=True):
    if dte<=0 or S<=0 or K<=0: return 0.0
    T=dte/365.0; r=0.065
    try:
        d1=(math.log(S/K)+(r+0.5*iv**2)*T)/(iv*math.sqrt(T))
        d2=d1-iv*math.sqrt(T)
        nc=lambda x:0.5*(1+math.erf(x/math.sqrt(2)))
        p=(S*nc(d1)-K*math.exp(-r*T)*nc(d2)) if call else (K*math.exp(-r*T)*nc(-d2)-S*nc(-d1))
        return max(0.0,round(p,2))
    except Exception as e: logger.debug(f"Black-Scholes failed S={S} K={K}: {e}"); return round(S*iv*math.sqrt(T)*0.4,2)

def select_strategy(underlying_price, max_pain, pcr, ce_walls, pe_walls, days_to_expiry, instrument, directional_bias="NEUTRAL", iv_estimate=0.15, iv_52w_high=0.35, iv_52w_low=0.10):
    iv_rank=((iv_estimate-iv_52w_low)/(iv_52w_high-iv_52w_low)*100) if iv_52w_high!=iv_52w_low else 50
    step=100 if underlying_price>40000 else 50
    atm=_strike(underlying_price,step)
    resistance=ce_walls[0] if ce_walls else atm+step*4
    support=pe_walls[0] if pe_walls else atm-step*4
    if days_to_expiry<=3 and abs(underlying_price-max_pain)/underlying_price<0.005:
        sc=_premium(underlying_price,atm,days_to_expiry,iv_estimate,True)
        sp=_premium(underlying_price,atm,days_to_expiry,iv_estimate,False)
        bc=_premium(underlying_price,atm+step*2,days_to_expiry,iv_estimate,True)
        bp=_premium(underlying_price,atm-step*2,days_to_expiry,iv_estimate,False)
        cr=sc+sp-bc-bp
        return OptionsSignal(strategy=OptionsStrategy.IRON_BUTTERFLY,instrument=instrument,sell_call_strike=atm,sell_put_strike=atm,buy_call_strike=atm+step*2,buy_put_strike=atm-step*2,sell_call_premium=sc,sell_put_premium=sp,total_credit=round(cr,2),max_profit=round(cr,2),max_loss=step*2-cr,breakeven_upper=atm+cr,breakeven_lower=atm-cr,stop_loss=cr*2,iv_rank=iv_rank,pcr=pcr,max_pain=max_pain,underlying_price=underlying_price,days_to_expiry=days_to_expiry,reason=f"Expiry week near max pain {max_pain:.0f}. Iron Butterfly at {atm}.",confidence="HIGH",valid=cr>0)
    if iv_rank>70 and directional_bias=="NEUTRAL":
        sc=_premium(underlying_price,atm,days_to_expiry,iv_estimate,True)
        sp=_premium(underlying_price,atm,days_to_expiry,iv_estimate,False)
        cr=sc+sp
        return OptionsSignal(strategy=OptionsStrategy.SHORT_STRADDLE,instrument=instrument,sell_call_strike=atm,sell_put_strike=atm,sell_call_premium=sc,sell_put_premium=sp,total_credit=cr,max_profit=cr,max_loss=underlying_price*0.05,breakeven_upper=atm+cr,breakeven_lower=atm-cr,stop_loss=cr*2,iv_rank=iv_rank,pcr=pcr,max_pain=max_pain,underlying_price=underlying_price,days_to_expiry=days_to_expiry,reason=f"IV Rank {iv_rank:.0f} high. Short Straddle at {atm}.",confidence="HIGH",valid=True)
    sc_k=atm+step*2; sp_k=atm-step*2
    sc=_premium(underlying_price,sc_k,days_to_expiry,iv_estimate,True)
    sp=_premium(underlying_price,sp_k,days_to_expiry,iv_estimate,False)
    cr=sc+sp
    if iv_rank>50:
        return OptionsSignal(strategy=OptionsStrategy.SHORT_STRANGLE,instrument=instrument,sell_call_strike=sc_k,sell_put_strike=sp_k,sell_call_premium=sc,sell_put_premium=sp,total_credit=cr,max_profit=cr,max_loss=underlying_price*0.08,breakeven_upper=sc_k+cr,breakeven_lower=sp_k-cr,stop_loss=cr*2,iv_rank=iv_rank,pcr=pcr,max_pain=max_pain,underlying_price=underlying_price,days_to_expiry=days_to_expiry,reason=f"Short Strangle: {sc_k}CE + {sp_k}PE. Credit={cr:.1f}",confidence="MEDIUM",valid=True)
    bc_k=sc_k+step*2; bp_k=sp_k-step*2
    bc=_premium(underlying_price,bc_k,days_to_expiry,iv_estimate,True)
    bp=_premium(underlying_price,bp_k,days_to_expiry,iv_estimate,False)
    cr2=(sc+sp)-(bc+bp)
    return OptionsSignal(strategy=OptionsStrategy.IRON_CONDOR,instrument=instrument,sell_call_strike=sc_k,sell_put_strike=sp_k,buy_call_strike=bc_k,buy_put_strike=bp_k,sell_call_premium=sc,sell_put_premium=sp,total_credit=round(cr2,2),max_profit=round(cr2,2),max_loss=round(step*2-cr2,2),breakeven_upper=sc_k+cr2,breakeven_lower=sp_k-cr2,stop_loss=cr2*2,iv_rank=iv_rank,pcr=pcr,max_pain=max_pain,underlying_price=underlying_price,days_to_expiry=days_to_expiry,reason=f"Iron Condor: {sc_k}CE/{sp_k}PE. Defined risk.",confidence="MEDIUM",valid=cr2>0)

def format_options_signal(sig, lots=1):
    if not sig.valid: return ""
    lot_size=50
    total=sig.total_credit*lots*lot_size
    lines=[f"\n⚙️ NON-DIRECTIONAL OPTIONS TRADE",f"Strategy  : {sig.strategy.value}",f"Instrument: {sig.instrument}","━━━━━━━━━━━━━━━━━━━━━━━"]
    if sig.sell_call_strike: lines.append(f"Sell CE   : {sig.sell_call_strike:.0f} @ ₹{sig.sell_call_premium:.1f}")
    if sig.sell_put_strike: lines.append(f"Sell PE   : {sig.sell_put_strike:.0f} @ ₹{sig.sell_put_premium:.1f}")
    if sig.buy_call_strike: lines.append(f"Buy  CE   : {sig.buy_call_strike:.0f} (hedge)")
    if sig.buy_put_strike: lines.append(f"Buy  PE   : {sig.buy_put_strike:.0f} (hedge)")
    lines+=[f"━━━━━━━━━━━━━━━━━━━━━━━",f"Total Credit : ₹{sig.total_credit:.1f}/lot",f"Max Profit   : ₹{total:.0f} ({lots} lot)",f"Stop Loss    : ₹{sig.stop_loss:.1f} debit",f"Upper BE     : {sig.breakeven_upper:.0f}",f"Lower BE     : {sig.breakeven_lower:.0f}",f"DTE          : {sig.days_to_expiry} days","━━━━━━━━━━━━━━━━━━━━━━━",f"📌 {sig.reason}"]
    return "\n".join(lines)
