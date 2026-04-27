"""Execution and risk management via direct Alpaca REST API calls.

Uses `requests` instead of alpaca-py so authentication is identical to
a plain curl call — this sidesteps the 403 errors the SDK can produce
with paper-account keys.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import requests

from .config import AppConfig
from .strategist import TradePlan

log = logging.getLogger(__name__)

PAPER_BASE = "https://paper-api.alpaca.markets/v2"
LIVE_BASE  = "https://api.alpaca.markets/v2"


@dataclass
class SizedPlan:
    plan: TradePlan
    qty: int
    est_premium: float  # total $ at risk at entry


class Executor:
    def __init__(self, cfg: AppConfig):
        self._cfg = cfg
        self._base = PAPER_BASE if cfg.alpaca_paper else LIVE_BASE
        self._headers = {
            "APCA-API-KEY-ID": cfg.alpaca_key,
            "APCA-API-SECRET-KEY": cfg.alpaca_secret,
            "Content-Type": "application/json",
        }

    # -- Raw HTTP helpers ----------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        r = requests.get(
            f"{self._base}{path}",
            headers=self._headers,
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = requests.post(
            f"{self._base}{path}",
            headers=self._headers,
            json=body,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    # -- Market clock --------------------------------------------------------

    def get_clock(self) -> dict:
        """Return the Alpaca clock dict (keys: is_open, next_open, next_close)."""
        return self._get("/clock")

    # -- Account helpers -----------------------------------------------------

    def account_equity(self) -> float:
        acct = self._get("/account")
        return float(acct["equity"])

    def open_option_positions(self) -> list[dict]:
        """All currently open option positions as raw dicts."""
        positions = self._get("/positions")
        return [p for p in positions if p.get("asset_class") == "us_option"]

    # -- Sizing --------------------------------------------------------------

    def size_plan(self, plan: TradePlan, equity: float) -> SizedPlan | None:
        """Compute contract quantity so stop-loss $ ≈ equity_risk_per_trade * equity."""
        risk_dollars = equity * self._cfg.risk.equity_risk_per_trade
        dollars_per_contract_at_risk = plan.limit_price * 100 * plan.stop_loss_pct
        if dollars_per_contract_at_risk <= 0:
            return None

        qty = max(1, math.floor(risk_dollars / dollars_per_contract_at_risk))

        premium_per_contract = plan.limit_price * 100
        max_premium = equity * self._cfg.risk.max_portfolio_allocation
        max_qty_by_premium = (
            math.floor(max_premium / premium_per_contract) if premium_per_contract > 0 else 0
        )
        if max_qty_by_premium < 1:
            log.info("Skipping %s — one contract exceeds portfolio cap", plan.option_symbol)
            return None
        qty = min(qty, max_qty_by_premium)

        return SizedPlan(plan=plan, qty=qty, est_premium=premium_per_contract * qty)

    # -- Entry ---------------------------------------------------------------

    def submit_entry(self, sized: SizedPlan) -> str | None:
        """Submit a day limit BUY_TO_OPEN for the chosen option."""
        if self._cfg.dry_run:
            log.info(
                "[dry-run] would buy %d x %s @ limit %.2f",
                sized.qty, sized.plan.option_symbol, sized.plan.limit_price,
            )
            return None

        body = {
            "symbol": sized.plan.option_symbol,
            "qty": str(sized.qty),
            "side": "buy",
            "type": "limit",
            "limit_price": str(round(sized.plan.limit_price, 2)),
            "time_in_force": "day",
            "order_class": "simple",
        }
        order = self._post("/orders", body)
        log.info(
            "entry submitted: %s qty=%d limit=%.2f id=%s",
            sized.plan.option_symbol, sized.qty, sized.plan.limit_price, order["id"],
        )
        return order["id"]

    # -- Exit sweep ----------------------------------------------------------

    def sweep_exits(self) -> list[str]:
        """Close any open option position that breached its TP/SL thresholds."""
        closed: list[str] = []
        risk = self._cfg.risk
        for pos in self.open_option_positions():
            try:
                avg_entry = float(pos.get("avg_entry_price") or 0)
                current   = float(pos.get("current_price") or avg_entry)
                if avg_entry <= 0:
                    continue
                pnl_pct = (current - avg_entry) / avg_entry

                reason = None
                if pnl_pct <= -risk.stop_loss_pct:
                    reason = f"stop-loss hit ({pnl_pct:.1%})"
                elif pnl_pct >= risk.take_profit_pct:
                    reason = f"take-profit hit ({pnl_pct:.1%})"

                if reason:
                    self._close_position(pos["symbol"], int(float(pos["qty"])), reason)
                    closed.append(pos["symbol"])
            except Exception as exc:  # noqa: BLE001
                log.exception("sweep error on %s: %s", pos.get("symbol"), exc)
        return closed

    def _close_position(self, symbol: str, qty: int, reason: str):
        if self._cfg.dry_run:
            log.info("[dry-run] would close %s qty=%d — %s", symbol, qty, reason)
            return
        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
        }
        order = self._post("/orders", body)
        log.info("close submitted: %s qty=%d reason=%s id=%s", symbol, qty, reason, order["id"])
