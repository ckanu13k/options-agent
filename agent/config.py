"""Configuration for the options trading agent.

All risk/sizing parameters live here so they're easy to tune without touching
strategy code. Defaults are intentionally conservative for a paper account.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    # Percent of account equity to risk per single trade (position sizing)
    equity_risk_per_trade: float = 0.02  # 2%

    # Max fraction of equity deployed across all open option positions
    max_portfolio_allocation: float = 0.20  # 20%

    # Close a losing position at this loss fraction of premium paid
    stop_loss_pct: float = 0.15  # 15%

    # Take profit at this gain fraction of premium paid
    take_profit_pct: float = 0.50  # 50%

    # Max number of concurrent open option positions
    max_open_positions: int = 5

    # Candidate filter: minimum average daily option volume we'll consider
    min_option_volume: int = 500

    # Candidate filter: minimum open interest
    min_open_interest: int = 500

    # Candidate filter: bid-ask spread width cap (as fraction of mid)
    max_spread_pct: float = 0.10  # 10%


@dataclass(frozen=True)
class ContractConfig:
    # Minimum days to expiration for contracts we'll buy
    min_dte: int = 7
    # Maximum days to expiration (keep theta decay manageable)
    max_dte: int = 45
    # Delta band for long calls/puts (directional, not too deep, not too cheap)
    min_abs_delta: float = 0.30
    max_abs_delta: float = 0.60


# Liquid, high-option-volume US underlyings. This is the shortlist the agent
# researches each run. Keep it tight so Claude can reason thoroughly on each.
DEFAULT_UNIVERSE: list[str] = [
    "SPY", "QQQ", "IWM",              # Index ETFs
    "AAPL", "MSFT", "NVDA", "AMZN",   # Mega-cap tech
    "GOOGL", "META", "TSLA",
    "AMD", "AVGO",                    # Semis
    "JPM", "BAC",                     # Banks
    "XOM",                            # Energy
]


@dataclass(frozen=True)
class AppConfig:
    alpaca_key: str
    alpaca_secret: str
    alpaca_paper: bool
    anthropic_key: str
    claude_model: str
    universe: list[str]
    risk: RiskConfig
    contract: ContractConfig
    dry_run: bool  # If True, plan only — don't submit orders

    @classmethod
    def from_env(cls) -> "AppConfig":
        missing = [
            k for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY")
            if not os.getenv(k)
        ]
        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

        return cls(
            alpaca_key=os.environ["ALPACA_API_KEY"],
            alpaca_secret=os.environ["ALPACA_SECRET_KEY"],
            alpaca_paper=os.getenv("ALPACA_PAPER", "true").lower() == "true",
            anthropic_key=os.environ["ANTHROPIC_API_KEY"],
            claude_model=os.getenv("CLAUDE_MODEL", "claude-opus-4-7"),
            universe=os.getenv("TICKER_UNIVERSE", ",".join(DEFAULT_UNIVERSE)).split(","),
            risk=RiskConfig(),
            contract=ContractConfig(),
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
        )
