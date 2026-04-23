"""Market data adapters wrapping alpaca-py.

Two responsibilities:
  1. Fetch underlying price action (recent bars) + news for each ticker so
     Claude can reason about the setup.
  2. Fetch an option chain filtered to our DTE/delta/liquidity bands so the
     agent only picks from tradable contracts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    OptionSnapshotRequest,
    StockBarsRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.trading.enums import AssetStatus, ContractType

from .config import AppConfig, ContractConfig, RiskConfig

log = logging.getLogger(__name__)


@dataclass
class TickerSnapshot:
    symbol: str
    last_price: float
    change_pct_1d: float
    change_pct_5d: float
    avg_volume_10d: float
    atr_pct: float  # average true range as % of price (volatility proxy)
    recent_news: list[dict] = field(default_factory=list)

    def as_brief(self) -> str:
        """Compact text brief for the LLM prompt."""
        headlines = "\n".join(
            f"  - [{n['created_at'][:10]}] {n['headline']}"
            for n in self.recent_news[:5]
        ) or "  (no recent headlines)"
        return (
            f"{self.symbol}: ${self.last_price:.2f} | "
            f"1d {self.change_pct_1d:+.2%} | 5d {self.change_pct_5d:+.2%} | "
            f"ATR {self.atr_pct:.2%} | avg vol {self.avg_volume_10d:,.0f}\n"
            f"Recent news:\n{headlines}"
        )


@dataclass
class OptionCandidate:
    symbol: str           # OCC option symbol, e.g. AAPL250516C00180000
    underlying: str
    contract_type: Literal["call", "put"]
    strike: float
    expiration: str       # ISO date
    dte: int
    bid: float
    ask: float
    mid: float
    delta: float | None
    iv: float | None
    volume: int
    open_interest: int

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid if self.mid > 0 else 1.0

    def as_brief(self) -> str:
        return (
            f"{self.symbol} | {self.contract_type.upper()} ${self.strike:.2f} "
            f"exp {self.expiration} ({self.dte}d) | "
            f"bid {self.bid:.2f}/ask {self.ask:.2f} (spread {self.spread_pct:.1%}) | "
            f"Δ {self.delta if self.delta is not None else 'n/a'} | "
            f"OI {self.open_interest} vol {self.volume}"
        )


class MarketData:
    def __init__(self, cfg: AppConfig):
        self._cfg = cfg
        self._stock = StockHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)
        self._option = OptionHistoricalDataClient(cfg.alpaca_key, cfg.alpaca_secret)
        self._news = NewsClient(cfg.alpaca_key, cfg.alpaca_secret)
        self._trading = TradingClient(cfg.alpaca_key, cfg.alpaca_secret, paper=cfg.alpaca_paper)

    # -- Underlying research -------------------------------------------------

    def snapshot_ticker(self, symbol: str) -> TickerSnapshot | None:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=20)
        try:
            bars_resp = self._stock.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
            ))
            bars = bars_resp.data.get(symbol, [])
            if len(bars) < 6:
                log.warning("Not enough bars for %s", symbol)
                return None

            closes = [b.close for b in bars]
            highs = [b.high for b in bars]
            lows = [b.low for b in bars]
            volumes = [b.volume for b in bars]

            last = closes[-1]
            chg_1d = (closes[-1] / closes[-2]) - 1
            chg_5d = (closes[-1] / closes[-6]) - 1 if len(closes) >= 6 else 0.0

            # Simple ATR% over last 10 sessions
            trs = [
                max(h - l, abs(h - pc), abs(l - pc))
                for h, l, pc in zip(highs[1:], lows[1:], closes[:-1])
            ][-10:]
            atr = sum(trs) / len(trs) if trs else 0.0
            atr_pct = atr / last if last else 0.0

            avg_vol = sum(volumes[-10:]) / max(len(volumes[-10:]), 1)

            news_items = self._fetch_news(symbol)

            return TickerSnapshot(
                symbol=symbol,
                last_price=last,
                change_pct_1d=chg_1d,
                change_pct_5d=chg_5d,
                avg_volume_10d=avg_vol,
                atr_pct=atr_pct,
                recent_news=news_items,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("snapshot_ticker failed for %s: %s", symbol, exc)
            return None

    def _fetch_news(self, symbol: str, limit: int = 10) -> list[dict]:
        try:
            resp = self._news.get_news(NewsRequest(
                symbols=symbol,
                start=datetime.now(timezone.utc) - timedelta(days=3),
                limit=limit,
            ))
            items = []
            for n in resp.data.get("news", []) or []:
                items.append({
                    "headline": n.headline,
                    "summary": (n.summary or "")[:400],
                    "created_at": n.created_at.isoformat() if hasattr(n, "created_at") else "",
                    "source": getattr(n, "source", ""),
                })
            return items
        except Exception as exc:  # noqa: BLE001
            log.warning("news fetch failed for %s: %s", symbol, exc)
            return []

    # -- Option chain --------------------------------------------------------

    def fetch_option_candidates(
        self,
        underlying: str,
        direction: Literal["call", "put"],
        spot: float,
        risk: RiskConfig,
        contract_cfg: ContractConfig,
    ) -> list[OptionCandidate]:
        """Return liquid, in-band option contracts for the requested direction."""
        min_exp = (datetime.now(timezone.utc) + timedelta(days=contract_cfg.min_dte)).date()
        max_exp = (datetime.now(timezone.utc) + timedelta(days=contract_cfg.max_dte)).date()

        req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            status=AssetStatus.ACTIVE,
            expiration_date_gte=min_exp,
            expiration_date_lte=max_exp,
            type=ContractType.CALL if direction == "call" else ContractType.PUT,
            # Restrict strikes to a ±15% band around spot; way OTM is cheap-but-lottery
            strike_price_gte=str(round(spot * 0.85, 2)),
            strike_price_lte=str(round(spot * 1.15, 2)),
            limit=100,
        )
        contracts = self._trading.get_option_contracts(req).option_contracts or []
        if not contracts:
            return []

        # Pull snapshots (bid/ask/greeks) for the filtered contract list
        occ_symbols = [c.symbol for c in contracts]
        snap_resp = self._option.get_option_snapshot(
            OptionSnapshotRequest(symbol_or_symbols=occ_symbols)
        )
        snaps = snap_resp  # dict of {symbol: OptionSnapshot}

        today = datetime.now(timezone.utc).date()
        out: list[OptionCandidate] = []
        for c in contracts:
            snap = snaps.get(c.symbol) if isinstance(snaps, dict) else None
            quote = getattr(snap, "latest_quote", None) if snap else None
            greeks = getattr(snap, "greeks", None) if snap else None

            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2
            delta = float(greeks.delta) if greeks and greeks.delta is not None else None
            iv = float(getattr(snap, "implied_volatility", 0) or 0) if snap else None

            oi = int(getattr(c, "open_interest", 0) or 0)
            vol = int(getattr(snap, "daily_bar", None).volume) if snap and getattr(snap, "daily_bar", None) else 0

            cand = OptionCandidate(
                symbol=c.symbol,
                underlying=underlying,
                contract_type=direction,
                strike=float(c.strike_price),
                expiration=str(c.expiration_date),
                dte=(c.expiration_date - today).days,
                bid=bid, ask=ask, mid=mid,
                delta=delta, iv=iv,
                volume=vol, open_interest=oi,
            )

            # Liquidity & delta filters
            if cand.spread_pct > risk.max_spread_pct:
                continue
            if oi < risk.min_open_interest:
                continue
            if delta is not None:
                abs_d = abs(delta)
                if abs_d < contract_cfg.min_abs_delta or abs_d > contract_cfg.max_abs_delta:
                    continue
            out.append(cand)

        # Sort: prefer tighter spread, then higher OI
        out.sort(key=lambda x: (x.spread_pct, -x.open_interest))
        return out[:8]  # cap options presented to the LLM
