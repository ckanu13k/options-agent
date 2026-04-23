"""Claude-powered strategist.

Two LLM steps per day:
  1. **Screen** the universe → pick up to 3 tickers with the strongest setup,
     and for each, propose a direction (call or put) with rationale.
  2. **Select contract** → given a filtered list of liquid option candidates
     for the chosen direction, pick the single best contract and an entry
     plan (limit price, target, stop).

Both steps return strict JSON so the orchestrator can act on them
deterministically. If parsing fails, we skip the trade — never fall back to
a hallucinated plan.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

from anthropic import Anthropic

from .config import AppConfig
from .market_data import OptionCandidate, TickerSnapshot

log = logging.getLogger(__name__)


@dataclass
class ScreenPick:
    symbol: str
    direction: Literal["call", "put"]
    conviction: float  # 0..1
    thesis: str


@dataclass
class TradePlan:
    option_symbol: str
    underlying: str
    direction: Literal["call", "put"]
    limit_price: float
    take_profit_pct: float
    stop_loss_pct: float
    rationale: str


SYSTEM_PROMPT = """You are a disciplined US options trader running a daily
paper-trading agent. You favor liquid, directional single-leg long calls or
long puts on large-cap underlyings with a clear near-term catalyst or
technical setup. You only recommend trades you would take with real money.

Non-negotiable rules:
- Only long calls or long puts (no spreads, no shorts, no naked writing).
- Expiration must be 7 to 45 days out.
- Reject setups where the news flow and price action disagree.
- If no ticker has a high-conviction setup, return an empty list.
- Output STRICT JSON only — no prose, no markdown fences.
- Never invent tickers or contracts that weren't in the input.
"""


class Strategist:
    def __init__(self, cfg: AppConfig):
        self._cfg = cfg
        self._client = Anthropic(api_key=cfg.anthropic_key)

    # -- Step 1: screen ------------------------------------------------------

    def screen_universe(self, snapshots: list[TickerSnapshot]) -> list[ScreenPick]:
        if not snapshots:
            return []

        brief = "\n\n".join(s.as_brief() for s in snapshots)
        user_msg = f"""Here are today's snapshots for the watch universe.
Price action is daily bars; news is the last 72 hours.

{brief}

Pick 0 to 3 tickers with the highest-conviction directional setup for the
next 1–3 weeks. For each, choose 'call' (bullish) or 'put' (bearish),
a conviction score between 0 and 1, and a concise thesis (<=40 words)
that references BOTH the price action and the news.

Respond with JSON of this exact shape:
{{
  "picks": [
    {{"symbol": "AAPL", "direction": "call", "conviction": 0.72,
      "thesis": "..."}}
  ]
}}
If no setup clears your bar, return {{"picks": []}}."""

        raw = self._call_claude(user_msg)
        data = _parse_json(raw)
        picks: list[ScreenPick] = []
        for p in data.get("picks", []) or []:
            try:
                picks.append(ScreenPick(
                    symbol=p["symbol"].upper(),
                    direction=p["direction"].lower(),
                    conviction=float(p["conviction"]),
                    thesis=p["thesis"],
                ))
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("skipping malformed pick %r: %s", p, exc)
        # Sort by conviction desc, keep top 3
        picks.sort(key=lambda x: x.conviction, reverse=True)
        return picks[:3]

    # -- Step 2: select contract --------------------------------------------

    def select_contract(
        self,
        pick: ScreenPick,
        snapshot: TickerSnapshot,
        candidates: list[OptionCandidate],
    ) -> TradePlan | None:
        if not candidates:
            return None

        chain = "\n".join(c.as_brief() for c in candidates)
        user_msg = f"""Selected setup:
  Ticker: {pick.symbol}
  Direction: {pick.direction}
  Conviction: {pick.conviction}
  Thesis: {pick.thesis}

Underlying context:
{snapshot.as_brief()}

Eligible option contracts (all already pass liquidity + delta filters):
{chain}

Pick the single best contract to buy, and propose:
- limit_price: your intended entry (use the mid or slightly better;
  never above the ask)
- take_profit_pct: fraction of premium gain to close the winner (0.25–1.0)
- stop_loss_pct: fraction of premium loss to close the loser
  (must be 0.10–0.20; house risk rule caps at 0.15)
- rationale: <=40 words tying contract choice to the thesis

Respond with strict JSON:
{{
  "option_symbol": "AAPL250516C00180000",
  "limit_price": 3.25,
  "take_profit_pct": 0.50,
  "stop_loss_pct": 0.15,
  "rationale": "..."
}}
If none of the contracts are attractive, respond with {{"skip": true}}."""

        raw = self._call_claude(user_msg)
        data = _parse_json(raw)
        if data.get("skip"):
            return None

        try:
            sym = data["option_symbol"]
            # Guard: must be a symbol from the candidate list
            chosen = next((c for c in candidates if c.symbol == sym), None)
            if chosen is None:
                log.warning("LLM returned unknown contract %s", sym)
                return None

            stop = float(data["stop_loss_pct"])
            # Hard cap at the configured house stop
            stop = min(stop, self._cfg.risk.stop_loss_pct)

            return TradePlan(
                option_symbol=sym,
                underlying=chosen.underlying,
                direction=chosen.contract_type,
                limit_price=float(data["limit_price"]),
                take_profit_pct=float(data["take_profit_pct"]),
                stop_loss_pct=stop,
                rationale=data.get("rationale", ""),
            )
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("malformed contract pick %r: %s", data, exc)
            return None

    # -- Internal ------------------------------------------------------------

    def _call_claude(self, user_msg: str) -> str:
        resp = self._client.messages.create(
            model=self._cfg.claude_model,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        return "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        )


def _parse_json(text: str) -> dict:
    """Tolerant JSON parser — strips markdown fences if the model slips up."""
    text = text.strip()
    # Strip ```json ... ``` fences just in case
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.error("failed to parse LLM JSON: %s", text[:500])
        return {}
