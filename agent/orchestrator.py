"""Top-level orchestration.

Single entrypoint: `run_once()`. The flow each run is:

  1. Check market is open (skip on holidays/weekends).
  2. Sweep existing positions for stop-loss / take-profit exits.
  3. Capacity check: are we already at max_open_positions?
  4. Build underlying snapshots for the universe.
  5. Ask Claude to screen → top tickers with direction.
  6. For each pick, fetch the filtered option chain, ask Claude to pick a
     contract + plan, size it, submit.
"""
from __future__ import annotations

import logging

from .config import AppConfig
from .executor import Executor
from .market_data import MarketData
from .strategist import Strategist

log = logging.getLogger(__name__)


def run_once() -> dict:
    cfg = AppConfig.from_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    md = MarketData(cfg)
    strat = Strategist(cfg)
    ex = Executor(cfg)

    summary = {"mode": "paper" if cfg.alpaca_paper else "live",
               "dry_run": cfg.dry_run, "closed": [], "opened": [],
               "skipped_reason": None}

    # 1. Market clock
    clock = ex._client.get_clock()
    if not clock.is_open:
        log.info("Market closed (next open %s) — sweeping only.", clock.next_open)
        # Still run the sweep in case we want to queue next-session exits;
        # otherwise just report and stop.
        summary["skipped_reason"] = "market_closed"
        return summary

    # 2. Exit sweep first — always honor risk rules before taking new risk
    summary["closed"] = ex.sweep_exits()

    # 3. Capacity check
    open_positions = ex.open_option_positions()
    capacity = cfg.risk.max_open_positions - len(open_positions)
    if capacity <= 0:
        log.info("At max open positions (%d) — no new entries.", len(open_positions))
        summary["skipped_reason"] = "at_capacity"
        return summary

    equity = ex.account_equity()
    log.info("Account equity=$%.2f capacity=%d", equity, capacity)

    # 4. Universe snapshots
    snapshots = []
    for sym in cfg.universe:
        snap = md.snapshot_ticker(sym.strip())
        if snap:
            snapshots.append(snap)
    if not snapshots:
        summary["skipped_reason"] = "no_snapshots"
        return summary

    # 5. Screen
    picks = strat.screen_universe(snapshots)
    log.info("Claude screened %d picks", len(picks))
    if not picks:
        summary["skipped_reason"] = "no_setups"
        return summary

    # 6. For each pick, build chain → select contract → size → submit
    snaps_by_sym = {s.symbol: s for s in snapshots}
    for pick in picks[:capacity]:
        snap = snaps_by_sym.get(pick.symbol)
        if not snap:
            continue
        candidates = md.fetch_option_candidates(
            underlying=pick.symbol,
            direction=pick.direction,
            spot=snap.last_price,
            risk=cfg.risk,
            contract_cfg=cfg.contract,
        )
        if not candidates:
            log.info("%s: no liquid contracts in band — skipping", pick.symbol)
            continue

        plan = strat.select_contract(pick, snap, candidates)
        if not plan:
            log.info("%s: Claude declined to pick a contract", pick.symbol)
            continue

        sized = ex.size_plan(plan, equity)
        if not sized or sized.qty < 1:
            log.info("%s: sizing produced 0 contracts — skipping", pick.symbol)
            continue

        order_id = ex.submit_entry(sized)
        summary["opened"].append({
            "ticker": pick.symbol,
            "option": plan.option_symbol,
            "direction": plan.direction,
            "qty": sized.qty,
            "limit": plan.limit_price,
            "est_premium": sized.est_premium,
            "tp_pct": plan.take_profit_pct,
            "sl_pct": plan.stop_loss_pct,
            "thesis": pick.thesis,
            "rationale": plan.rationale,
            "order_id": order_id,
        })

    log.info("Run complete: %s", summary)
    return summary


if __name__ == "__main__":
    run_once()
