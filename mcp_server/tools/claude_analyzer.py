"""
tools/claude_analyzer.py — Claude-powered SMC trade evaluation.

Replaces the rigid point-scoring gate with a contextual Claude API call.
Claude receives the full institutional analysis + HTF structure and decides
whether to send a signal, what grade to assign, and writes the narrative.

Falls back to decision_engine.score_decision() transparently if the API is
unavailable.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional

import httpx

from mcp_server.config import get_settings

logger = logging.getLogger(__name__)
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
# Haiku: fast (~0.8s), cheap ($0.25/M in, $1.25/M out), good enough for structured output
CLAUDE_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class ClaudeDecision:
    send: bool
    direction: str              # "LONG" | "SHORT"
    grade: str                  # "A+" | "A" | "B" | "C"
    confidence: float           # 0-100 (used as score)
    narrative: str
    key_reasons: List[str] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.confidence


async def evaluate_setup(
    symbol: str,
    segment: str,
    timeframe: int,
    current_price: float,
    closes: list,
    highs: list,
    lows: list,
    volumes: list,
    inst_bias: str,
    inst_score: float,
    inst_evidence: list,
    liquidity_event: str,
    breaker_block: bool,
    propulsion_block: bool,
    mitigation_block: bool,
    wyckoff_phase: str,
    weekly_trend: str,
    daily_structure: str,
    h4_flow: str,
    in_discount: bool,
    is_killzone: bool,
    ltf_choch: bool,
    volume_spike: bool,
    options_data: Optional[dict] = None,
) -> Optional[ClaudeDecision]:
    """
    Ask Claude to evaluate whether this SMC setup is worth signaling.

    Returns ClaudeDecision if API succeeds, None on failure (caller falls back
    to decision_engine.score_decision()).
    """
    settings = get_settings()
    api_key = getattr(settings, "anthropic_api_key", None)
    if not api_key:
        logger.debug("No ANTHROPIC_API_KEY — skipping Claude evaluation")
        return None

    # ── Compact price-action summary ──────────────────────────────
    n = len(closes)
    change_5  = round((closes[-1] - closes[-5])  / closes[-5]  * 100, 2) if n >= 5  else 0.0
    change_20 = round((closes[-1] - closes[-20]) / closes[-20] * 100, 2) if n >= 20 else 0.0
    avg_vol   = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else (volumes[-1] if volumes else 1)
    vol_ratio = round(volumes[-1] / avg_vol, 1) if avg_vol > 0 else 1.0

    # ── Options summary (one line if present) ────────────────────
    opts_line = ""
    if options_data and options_data.get("pcr"):
        pcr  = options_data["pcr"]
        mp   = options_data.get("max_pain", "N/A")
        bias = options_data.get("options_direction", "NEUTRAL")
        opts_line = f"\nOPTIONS: PCR={pcr:.2f} | MaxPain={mp} | Bias={bias}"

    prompt = f"""You are a precise SMC (Smart Money Concepts) trading analyst.
Evaluate this setup and decide whether to generate a trade signal.

INSTRUMENT: {symbol} | {segment} | {timeframe}m
PRICE: {current_price:.2f}  ({change_5:+.2f}% 5-bar | {change_20:+.2f}% 20-bar)
VOLUME: {vol_ratio:.1f}x average

HTF STRUCTURE:
  Weekly : {weekly_trend}
  Daily  : {daily_structure}
  H4     : {h4_flow}
  Zone   : {"Discount ✓ (buying zone)" if in_discount else "Premium (selling zone)"}

INSTITUTIONAL ANALYSIS (score {inst_score:.0f}/100):
  Bias            : {inst_bias}
  Liquidity Event : {liquidity_event}
  Order Block     : {"FRESH ✓" if not mitigation_block else "MITIGATED ✗"}
  OB+FVG Overlap  : {propulsion_block}
  Breaker Block   : {breaker_block}
  Wyckoff         : {wyckoff_phase}
  Evidence        : {inst_evidence[:4]}{opts_line}

ENTRY TRIGGERS:
  Killzone : {is_killzone}
  LTF CHOCH: {ltf_choch}
  Vol Spike: {volume_spike}

Evaluate strictly. Signal only if:
1. HTF structure and entry direction agree
2. A fresh or breaker OB (or OB+FVG) is present
3. At least one entry trigger is active

Respond ONLY with valid JSON, no markdown fences:
{{"send":true/false,"direction":"LONG or SHORT","grade":"A+ or A or B or C","confidence":50-100,"narrative":"2 sentences max","key_reasons":["reason1","reason2"],"risk_factors":["risk1"]}}"""

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                ANTHROPIC_API,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 350,
                    "temperature": 0,            # deterministic, consistent decisions
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

        if resp.status_code != 200:
            logger.warning(f"Claude API {resp.status_code} for {symbol}: {resp.text[:150]}")
            return None

        raw = resp.json()["content"][0]["text"].strip()

        # Strip accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw)

        decision = ClaudeDecision(
            send=bool(result.get("send", False)),
            direction=str(result.get("direction", "LONG")).upper(),
            grade=str(result.get("grade", "B")),
            confidence=float(result.get("confidence", 50)),
            narrative=str(result.get("narrative", "")),
            key_reasons=list(result.get("key_reasons", [])),
            risk_factors=list(result.get("risk_factors", [])),
        )

        logger.info(
            f"Claude [{symbol} {timeframe}m]: send={decision.send} "
            f"grade={decision.grade} confidence={decision.confidence:.0f} "
            f"dir={decision.direction}"
        )
        return decision

    except json.JSONDecodeError as e:
        logger.warning(f"Claude response not JSON for {symbol}: {e} | raw={raw[:100]}")
        return None
    except Exception as e:
        logger.error(f"Claude API failed for {symbol}: {e}")
        return None
