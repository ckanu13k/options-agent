# Daily Options Trading Agent

LLM-driven options agent that screens a liquid US equity universe daily,
selects a long call or long put with a 7–45 day expiration, and manages
the position with strict stop-loss / take-profit rules. Runs on a schedule
via GitHub Actions against an Alpaca **paper** account.

> ⚠️ **Paper trading only.** This codebase is a research / educational
> scaffold. Options carry substantial risk and can expire worthless. Do not
> flip to a live account without a lot of validation and your own judgment.
> Nothing here is financial advice.

---

## How it works

Two scheduled workflows:

| Workflow | Frequency | Job |
|---|---|---|
| `daily.yml` | 10:00 ET on weekdays | Screen → pick → enter new positions |
| `sweep.yml` | Every 30 min during market hours | Close anything past TP/SL |

Each daily run:

1. Check the market is open (skips holidays/weekends automatically).
2. Sweep exits first — honor risk rules before taking new risk.
3. Pull 20 trading days of bars + 72h of headlines for each ticker in the
   universe (`agent/config.py::DEFAULT_UNIVERSE`).
4. Ask Claude (system prompt in `agent/strategist.py`) to pick up to 3
   tickers with a clear directional setup, returning strict JSON.
5. For each pick, fetch the option chain filtered to:
   - DTE in `[7, 45]`
   - `|delta|` in `[0.30, 0.60]`
   - OI ≥ 500, bid-ask spread ≤ 10% of mid
   - strikes within ±15% of spot
6. Ask Claude to select one contract and propose a `limit_price`,
   `take_profit_pct`, and `stop_loss_pct` (house stop caps it at 15%).
7. Size the position so the stop-loss $ ≈ 2% of account equity, capped by
   a 20% total-premium portfolio allocation.
8. Submit a day limit BUY-TO-OPEN order.

Risk parameters all live in `agent/config.py::RiskConfig` — tune them there.

---

## Project layout

```
options-agent/
├── agent/
│   ├── __init__.py
│   ├── __main__.py          # `python -m agent`
│   ├── config.py            # Risk + contract config, universe, env loader
│   ├── market_data.py       # Bars, news, option chain from Alpaca
│   ├── strategist.py        # Claude screen + contract-select prompts
│   ├── executor.py          # Sizing, order submission, exit sweep
│   └── orchestrator.py      # run_once() top-level flow
├── tests/
│   └── test_logic.py
├── .github/workflows/
│   ├── daily.yml            # Entry workflow (daily, 10:00 ET)
│   └── sweep.yml            # Exit-sweep workflow (every 30 min)
├── requirements.txt
├── .env.example
└── README.md
```

---

## Setup

### 1. Alpaca paper account

1. Sign up at <https://alpaca.markets>.
2. Open <https://app.alpaca.markets/paper/dashboard/overview>.
3. Generate a paper API key pair.
4. Options trading is **enabled by default** on paper accounts — nothing
   extra to do.

### 2. Anthropic API key

Get one at <https://console.anthropic.com>.

### 3. Local smoke test

```bash
git clone <your-fork-url> options-agent
cd options-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env with your keys

# Dry-run first — screens and plans but submits no orders
DRY_RUN=true python -m agent
```

You should see JSON output ending in an `opened` list (or
`skipped_reason`). Once that looks sane, flip `DRY_RUN=false` to let it
actually submit orders to the paper account.

### 4. Run the tests

```bash
pip install pytest
pytest -q
```

### 5. Deploy to GitHub Actions

1. Push this repo to GitHub.
2. In **Settings → Secrets and variables → Actions**, add three secrets:
   - `ALPACA_API_KEY`
   - `ALPACA_SECRET_KEY`
   - `ANTHROPIC_API_KEY`
3. Go to the **Actions** tab, enable workflows, and trigger
   `daily-options-agent` manually once (with `dry_run=true`) to confirm it
   runs end-to-end.
4. The cron schedules will then run automatically on weekdays.

---

## Configuration knobs

All in `agent/config.py`:

```python
equity_risk_per_trade = 0.02   # 2% equity at risk per position
max_portfolio_allocation = 0.20 # 20% total premium cap
stop_loss_pct = 0.15            # Close losers at −15% of premium
take_profit_pct = 0.50          # Close winners at +50% of premium
max_open_positions = 5
min_dte, max_dte = 7, 45
min_abs_delta, max_abs_delta = 0.30, 0.60
```

You can also override the universe without editing code:

```bash
TICKER_UNIVERSE=SPY,QQQ,AAPL,NVDA python -m agent
```

---

## The Claude prompt (summarized)

```
You are a disciplined US options trader running a daily paper-trading
agent. You favor liquid, directional single-leg long calls or long puts
on large-cap underlyings with a clear near-term catalyst or technical
setup. You only recommend trades you would take with real money.

Non-negotiable rules:
- Only long calls or long puts (no spreads, no shorts, no naked writing).
- Expiration must be 7 to 45 days out.
- Reject setups where the news flow and price action disagree.
- If no ticker has a high-conviction setup, return an empty list.
- Output STRICT JSON only.
- Never invent tickers or contracts that weren't in the input.
```

Two structured calls per run: `screen_universe` → `select_contract`.
Both outputs are validated against the input data (e.g. the contract
symbol must be in the candidate list), so a hallucinated pick is dropped
rather than executed.

---

## Known limitations

- **No backtest.** The agent is designed for forward-testing on paper.
  Before trusting it, run it for several weeks and compare the LLM's
  theses to what actually happened.
- **News latency.** Alpaca News headlines may lag specialized feeds.
- **IEX-only data on paper.** Paper accounts receive IEX data, which may
  differ slightly from SIP. Quote resolution for options snapshots uses
  the options feed Alpaca provides to paper accounts.
- **GitHub Actions scheduling jitter.** Scheduled workflows can run
  several minutes late under high platform load. The market-clock check
  prevents off-hours trades but can't compensate for, say, a 20-min
  delayed fill attempt.
- **No multi-leg strategies.** The scaffold deliberately limits itself to
  single-leg long options. Adding spreads would require changes in the
  prompt, candidate builder, and executor.
