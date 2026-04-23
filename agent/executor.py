"""Execution and risk management against the Alpaca account.

Responsibilities:
  * Size positions by equity-risk % given the LLM's stop distance.
  * Submit entries as day limit orders.
  * Sweep open positions each run and close anything that has hit its
    configured stop or take-profit.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from .config import AppConfig
from .strategist import TradePlan

log = logging.getLogger(__name__)


@dataclass
class SizedPlan:
    plan: TradePlan
    qty: int
    est_premium: float  # total $ at risk at entry


class Executor:
    def __init__(self, cfg: AppConfig):
        self._cfg = cfg
        self._client = TradingClient(cfg.alpaca_key, cfg.alpaca_secret, paper=cfg.alpaca_paper)

    # -- Account helpers -----------------------------------------------------

    def account_equity(self) -> float:
        acct = self._client.get_account()
        return float(acct.equity)

    def open_option_positions(self):
        """All currently open option positions."""
        positions = self._client.get_all_positions()
        return [p for p in positions if p.asset_class == "us_option"]

    # -- Sizing --------------------------------------------------------------

    def size_plan(self, plan: TradePlan, equity: float) -> SizedPlan | None:
        """Compute contract quantity so stop-loss $ ≈ equity_risk_per_trade * equity.

        Options are quoted per-share but trade per 100-share contract.
        Dollar risk per contract = limit_price * 100 * stop_loss_pct.
        """
        risk_dollars = equity * self._cfg.risk.equity_risk_per_trade
        dollars_per_contract_at_risk = plan.limit_price * 100 * plan.stop_loss_pct
        if dollars_per_contract_at_risk <= 0:
            return None

        qty = max(1, math.floor(risk_dollars / dollars_per_contract_at_risk))

        # Cap by total premium paid against portfolio allocation cap
        premium_per_contract = plan.limit_price * 100
        max_premium = equity * self._cfg.risk.max_portfolio_allocation
        max_qty_by_premium = math.floor(max_premium / premium_per_contract) if premium_per_contract > 0 else 0
        if max_qty_by_premium < 1:
            log.info("Skipping %s — one contract exceeds portfolio cap", plan.option_symbol)
            return None
        qty = min(qty, max_qty_by_premium)

        return SizedPlan(plan=plan, qty=qty, est_premium=premium_per_contract * qty)

    # -- Entry ---------------------------------------------------------------

    def submit_entry(self, sized: SizedPlan) -> str | None:
        """Submit a day limit BUY_TO_OPEN for the chosen option."""
        if self._cfg.dry_run:
            log.info("[dry-run] would buy %d x %s @ limit %.2f",
                     sized.qty, sized.plan.option_symbol, sized.plan.limit_price)
            return None

        req = LimitOrderRequest(
            symbol=sized.plan.option_symbol,
            qty=sized.qty,
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            limit_price=round(sized.plan.limit_price, 2),
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.SIMPLE,
        )
        order = self._client.submit_order(req)
        log.info("entry submitted: %s qty=%d limit=%.2f id=%s",
                 sized.plan.option_symbol, sized.qty, sized.plan.limit_price, order.id)
        return str(order.id)

    # -- Exit sweep ----------------------------------------------------------

    def sweep_exits(self) -> list[str]:
        """Close any open option position that breached its TP/SL thresholds.

        We store no local state — the house-level stop/TP rules from
        RiskConfig are applied to every open option position each run.
        """
        closed: list[str] = []
        risk = self._cfg.risk
        for pos in self.open_option_positions():
            try:
                avg_entry = float(pos.avg_entry_price)
                current = float(pos.current_price or pos.avg_entry_price)
                if avg_entry <= 0:
                    continue
                pnl_pct = (current - avg_entry) / avg_entry

                reason = None
                if pnl_pct <= -risk.stop_loss_pct:
                    reason = f"stop-loss hit ({pnl_pct:.1%})"
                elif pnl_pct >= risk.take_profit_pct:
                    reason = f"take-profit hit ({pnl_pct:.1%})"

                if reason:
                    self._close_position(pos.symbol, int(pos.qty), reason)
                    closed.append(pos.symbol)
            except Exception as exc:  # noqa: BLE001
                log.exception("sweep error on %s: %s", pos.symbol, exc)
        return closed

    def _close_position(self, symbol: str, qty: int, reason: str):
        if self._cfg.dry_run:
            log.info("[dry-run] would close %s qty=%d — %s", symbol, qty, reason)
            return
        # Market sell-to-close. Options don't support closing via
        # `close_position` for all cases; a market order is safest.
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
        )
        order = self._client.submit_order(req)
        log.info("close submitted: %s qty=%d reason=%s id=%s", symbol, qty, reason, order.id)
