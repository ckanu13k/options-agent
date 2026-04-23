"""Unit tests covering the pure-logic pieces.

No network calls — we stub out the Alpaca/Anthropic clients so these can
run in CI without secrets.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

# Make the package importable without installation
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Minimal env so AppConfig.from_env() works
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from agent.config import AppConfig, ContractConfig, RiskConfig  # noqa: E402
from agent.strategist import TradePlan, _parse_json  # noqa: E402


def _cfg() -> AppConfig:
    return AppConfig.from_env()


def test_parse_json_plain():
    assert _parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_with_fence():
    raw = '```json\n{"picks": []}\n```'
    assert _parse_json(raw) == {"picks": []}


def test_parse_json_garbage_returns_empty():
    assert _parse_json("not json at all") == {}


def test_sizing_respects_risk_budget():
    # Lazy import so Executor construction (which needs Alpaca creds) is deferred
    from agent.executor import Executor

    class FakeClient:
        def __init__(self, *a, **k): pass
    # Monkey-patch TradingClient for this test
    import agent.executor as ex_mod
    ex_mod.TradingClient = FakeClient
    ex = Executor(_cfg())

    plan = TradePlan(
        option_symbol="AAPL250516C00180000",
        underlying="AAPL",
        direction="call",
        limit_price=3.00,
        take_profit_pct=0.50,
        stop_loss_pct=0.15,
        rationale="test",
    )
    # With $100k equity, 2% risk = $2000; stop risk per contract = 3*100*0.15 = $45
    # Expect qty ≈ 44 (2000 / 45) — but portfolio cap is 20% * 100k = 20k
    # and premium per contract = $300, so cap = 66. 44 < 66 → qty = 44.
    sized = ex.size_plan(plan, equity=100_000)
    assert sized is not None
    assert sized.qty == 44


def test_sizing_capped_by_portfolio_allocation():
    from agent.executor import Executor
    import agent.executor as ex_mod

    class FakeClient:
        def __init__(self, *a, **k): pass
    ex_mod.TradingClient = FakeClient
    ex = Executor(_cfg())

    plan = TradePlan(
        option_symbol="X", underlying="X", direction="call",
        limit_price=10.00, take_profit_pct=0.50, stop_loss_pct=0.15,
        rationale="test",
    )
    # With $10k equity, risk budget = $200; stop risk/contract = 10*100*0.15 = $150
    # Risk-based qty = 1. Portfolio cap = 20% * 10k = 2000; premium/contract = 1000
    # Cap-based qty = 2. min(1, 2) = 1.
    sized = ex.size_plan(plan, equity=10_000)
    assert sized is not None
    assert sized.qty == 1


def test_sizing_rejects_when_single_contract_exceeds_cap():
    from agent.executor import Executor
    import agent.executor as ex_mod

    class FakeClient:
        def __init__(self, *a, **k): pass
    ex_mod.TradingClient = FakeClient
    ex = Executor(_cfg())

    # Expensive contract vs. small account: 1 contract = $5000, cap = 20% * 10k = $2000
    plan = TradePlan(
        option_symbol="X", underlying="X", direction="call",
        limit_price=50.00, take_profit_pct=0.50, stop_loss_pct=0.15,
        rationale="test",
    )
    sized = ex.size_plan(plan, equity=10_000)
    assert sized is None


def test_defaults_are_conservative():
    r = RiskConfig()
    assert r.equity_risk_per_trade <= 0.05
    assert r.stop_loss_pct <= 0.20
    c = ContractConfig()
    assert c.min_dte >= 7
    assert c.min_abs_delta < c.max_abs_delta
