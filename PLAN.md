# AutoFund: a digital investment fund completely run by AI agents
## Full Architecture & Operational Specification — v3

---

## Abstract

This document is the complete architectural and operational specification for AutoFund, a digital investment fund completely run by multiple AI agents. Each agent is specialized in a specific stage of the investment research pipeline. The fund operates autonomously: agents read financial news, analyze opportunities, build scenario models, and manage a live simulated portfolio. A programmatic monitoring layer enforces sell conditions automatically without requiring the Manager to run continuously.

---

## 1. General Architecture

The system operates a **fundamentals/events-driven investment fund**, where AI agents:
1. Read financial news and macroeconomic data from live sources
2. Pull real-time market data via Yahoo Finance (yfinance)
3. Build independent bull / base / bear scenarios for each opportunity
4. Make final buy/sell/hold/rebalance decisions
5. Execute trades in a paper trading engine at live market prices
6. Enforce sell conditions automatically on every open position via a background monitoring loop

### Core Design Principle

**Sell conditions live alongside the position in the portfolio state.** When the Manager buys an asset, it also writes one or more structured sell conditions. A background monitoring loop continuously evaluates all conditions and executes sells programmatically when they are triggered — no agent re-invocation required between buy and sell.

### LLM Infrastructure

All agents access the LLM via an **OpenAI-compatible endpoint through OpenRouter**. Every agent's system prompt and tool definitions are stored in `prompts/` and invoked through this shared endpoint.

**Model configuration file:** `config/models.json`

Each agent's assigned model is defined in `config/models.json`:

```json
{
  "economist":  "perplexity/sonar",
  "researcher": "qwen/qwen3-235b-a22b-2507",
  "analyst":    "minimax/minimax-m2.5",
  "associates": "qwen/qwen3-235b-a22b-2507",
  "manager":    "qwen/qwen3-235b-a22b-2507"
}
```

To change which model an agent uses, edit `config/models.json`. No code changes required.

---

## 2. The Investment Research Pipeline

The pipeline runs in six stages daily. Each stage's output feeds the next.

### Stage 0 — Economist (Macro Briefing)
**Agent:** Economist
**Trigger:** Once per trading day, before all other agents
**Tool used:** Sonar (Perplexity API) — a reasoning model with native web search capability

The Economist prompts Sonar to read live macroeconomic sources and produces a structured macro briefing covering:

- **Macro Regime** — e.g. Risk-On, Reflationary, Goldilocks, Stagflationary, Recessionary
- **Key Themes** — The 4–7 most important macro forces active today
- **Sector Signals** — Directional signal (tailwind / headwind / neutral) for each major equity sector
- **Risk Flags** — Geopolitical, policy, or financial stability risks with market-moving potential
- **Rate & Commodity Environment** — What rates and key commodities are signaling
- **Sources Consulted** — URLs actually retrieved

This briefing is injected as shared context into every downstream agent (Analyst, Associates, Manager).

**Output file:** `research/economist_briefing_YYYY-MM-DD.md` (dated, new file per trading day)

---

### Stage 1 — Researcher (News Scanning)
**Agent:** Researcher
**Trigger:** Every trading day after the Economist briefing
**Tool used:** `scraper/yfinance_scraper.py` — Playwright-based scraper that collects headlines and full article content from the Yahoo Finance homepage

The Researcher receives the JSON output from the scraper run and surfaces opportunities from those articles with:
- **Headline/Event** — The news item driving the opportunity
- **Affected Assets** — Stocks, commodities, ETFs, sectors
- **Key Findings** — 2–5 bullet points
- **Relevance Score** — 1–10 self-assessed
- **Notes** — Caveats or outstanding questions for the next stage

**Rule:** The Researcher does NOT make investment recommendations. It surfaces and scores; the pipeline decides what to act on.

**Output file:** `research/researcher_daily_YYYY-MM-DD.md` (dated, new file per trading day)

---

### Stage 2 — Analyst (Financial Analysis)
**Agent:** Analyst
**Trigger:** After Researcher surfaces opportunities AND Economist briefing is available

**Tools used:**
- `analyst/market_data.py` — yfinance data puller
- `analyst/models.py` — Pre-built financial calculation library (DCF, projections, technicals, peer comparisons)
- `run_python` — Code interpreter for custom/one-off analysis when the model library is insufficient

---

#### The Hybrid Calculation Architecture

The Analyst operates with a **three-layer calculation stack**:

```
Layer 1 — Standard Models (analyst/models.py)
  → Pre-built, deterministic financial functions
  → Called with typed parameters, return structured JSON
  → Always attempted first

Layer 2 — Code Interpreter (run_python tool)
  → Sandboxed arbitrary Python execution
  → Used when Layer 1 doesn't cover the specific analysis needed
  → Agent writes custom calculation code on the fly

Layer 3 — Prompt with Embedded Examples
  → Few-shot examples in the Analyst system prompt
  → Shows how and when to call each model
  → Includes expected output formats
```

**Workflow per opportunity:**
```
1. Pull raw data via market_data.py
2. Call Layer 1 standard models (DCF, technicals, peer multiples, projections)
3. If the opportunity requires a non-standard model (e.g., LBO, levered DCF,
   sector-specific multiples, event-driven analysis):
       → write custom Python via run_python()
4. Synthesize all model outputs into the analyst report
5. Save structured output to research/analyst_<ticker>.json
```

---

#### Layer 1 — Standard Model Library (`analyst/models.py`)

Each function supports two calling patterns:

- **Ticker-based** (recommended): pass `ticker="AAPL"` — the function fetches all required data internally from Yahoo Finance via `market_data.py`, no manual parameter extraction needed
- **Explicit**: pass parameters directly — no network calls, useful when you already have the numbers

| Function | Returns | Auto-derives from ticker |
|---|---|---|
| `dcf_valuation(ticker=)` | Intrinsic value, DCF steps, sensitivity table | FCF/share, 3-yr FCF CAGR, WACC from beta, shares outstanding |
| `projected_income_statement(ticker=)` | Year-by-year P&L projection DataFrame | Revenue from income statement, growth from 3-yr CAGR, opex ratio from operating margins |
| `technical_summary(ticker=)` | RSI, MACD, SMA 20/50/200, Bollinger, ATR, signals | Fetches 1 year of daily price history |
| `peer_valuation_multiples(ticker=, peer_tickers=)` | Multiples comparison table (P/E, EV/EBITDA, P/B) | Current price, P/E, EV/EBITDA, P/B for all tickers |
| `scenario_model(ticker=)` | Bull/base/bear price targets + assumptions | Auto-derives base assumptions; builds bull/bear variants |
| `dividend_discount_model(ticker=)` | Intrinsic value via DDM | Current price, dividend/share, dividend growth from 3-yr CAGR, required return via CAPM |
| `capm(rf, market, beta)` | Required return on equity | — (all inputs explicit) |
| `compound_growth_rate(beg, end, years)` | CAGR | — (all inputs explicit) |

**Calling convention examples:**
```python
# Ticker-based: function fetches data internally (recommended — saves tokens)
dcf = dcf_valuation(ticker="AAPL")
# dcf["intrinsic_value_per_share"] → e.g. 212.50
# dcf["upside_pct"]               → e.g. 18.3
# dcf["sensitivity_table"]       → table at different WACC/growth combos

# Explicit: all parameters provided (no network calls)
dcf = dcf_valuation(
    fcf_per_share=8.42,
    growth_rate=0.12,
    terminal_growth=0.025,
    wacc=0.09,
    shares_outstanding=15_500_000_000
)

# Overriding one auto-derived value while using ticker-based for the rest
dcf_custom = dcf_valuation(ticker="AAPL", terminal_growth=0.03)
```

---

#### Layer 2 — Code Interpreter (`run_python` tool)

```python
{
    "name": "run_python",
    "description": "Execute arbitrary Python code in a sandboxed environment. Use for custom calculations, projections, and data analysis not covered by analyst/models.py.",
    "parameters": {
        "code": "str",
        "imports": ["numpy", "pandas", "math", "statistics", "yfinance"]
    }
}
```

**Allowed extra imports:** `numpy`, `pandas`, `math`, `statistics`, `datetime`, `timezone`, `yfinance`

**Pre-loaded context available to the code:**
- `pull_ticker_data(symbol, modules=None, period="1y", interval="1d")` — pull raw yfinance data, auto-populates the vars below
- `snapshot` — full snapshot dict from market_data.py
- `financials` — income statement, balance sheet, cash flow from market_data.py
- `history` — OHLCV DataFrame + stats from market_data.py
- `analysis` — analyst data from market_data.py
- `math`, `statistics` — standard math libraries
- Any variables defined in prior calls within the same session

**When to use Layer 2 instead of Layer 1:**
- Special situation analysis (restructuring, M&A, spinoff) not covered by standard models
- Sector-specific valuation (e.g., asset-based valuation for insurers, EV/gigawatt for utilities)
- Custom scenario assumptions not supported by `scenario_model()`
- Non-standard accounting normalization that requires custom data cleaning

---

#### Data Inputs and yfinance Modules

| Analysis Type | Layer | yfinance Modules to Pull |
|---|---|---|
| DCF | Layer 1 | `snapshot` (FCF, shares, beta), `financials` (FCF history) |
| Income projection | Layer 1 | `financials` (revenue, opex), `analysis` (growth estimates) |
| Technical analysis | Layer 1 | `history`, `technicals` |
| Relative valuation | Layer 1 | `snapshot`, `analysis` (analyst targets) |
| Quality assessment | Layer 1 | `snapshot` (margins, ROE, debt/equity), `holders` |
| Scenario stress-testing | Layer 1 | `snapshot`, `history`, `analysis` |
| Special situations | Layer 2 | Custom — analyst determines which modules to pull |

The Analyst can pull multiple tickers at once. Data is saved to `research/analyst_<ticker>.json`.

---

#### Macro Consistency Check

Before issuing a recommendation, the Analyst explicitly assesses:
- Does the opportunity's micro thesis align with the Economist's macro regime?
- An opportunity that contradicts the macro backdrop (e.g., a cyclical value play in a Recessionary regime) must be flagged as **higher risk**, even if the micro thesis is strong
- The macro consistency assessment is included in the output under `macro_consistency`

**Output:** Structured analyst report including:
- Opportunity summary and macro context alignment
- Key model outputs (DCF intrinsic value, technical signals, peer comparison, scenario targets)
- All model assumptions transparently stated
- Macro consistency verdict
- Outlook: **Bullish / Bearish / Neutral** with justification
- Confidence level: **High / Medium / Low**
- Recommendation: **proceed to Associates / hold for later / dismiss**

---

### Stage 3 — Associates (Scenario Modeling)
**Agents:** 1× Base Scenario Associate, 1× Bearish Scenario Associate, 1× Bullish Scenario Associate
**Trigger:** After Analyst issues a positive recommendation
**Skill used:** Implicit — they reason over the Analyst's report and Economist's briefing

Each Associate independently builds a detailed scenario covering:
- **Scenario Type** — Base / Bearish / Bullish
- **Key Assumptions** — Conditions that must be true
- **Price Target** — Specific point estimate or range at scenario end
- **Timeframe** — Expected duration (e.g. 3–6 months, 12–18 months)
- **Catalysts** — Events confirming the scenario is underway
- **Risks to Scenario** — What could break or derail it
- **Macro Regime Alignment** — How the Economist's regime view affects this scenario
- **Conviction Level** — High / Medium / Low
- **Supporting Rationale** — 2–4 paragraphs

**Rule:** Associates do NOT interact with each other. Each produces an independent scenario. The Manager sees all 3 scenarios and weighs them.

**Output files:** `research/associate_base_YYYY-MM-DD.md`, `research/associate_bear_YYYY-MM-DD.md`, `research/associate_bull_YYYY-MM-DD.md`

---

### Stage 4 — Manager (Final Decision)
**Agent:** Manager
**Trigger:** After all 3 Associate scenarios are available
**Skill used:** `trader` (PaperTrader Python API)

The Manager reads all prior output and synthesizes a decision. Possible decisions:

| Decision | Meaning |
|---|---|
| **BUY** | Initiate a new position |
| **SELL** | Close or reduce an existing position |
| **HOLD** | Maintain current state — do nothing |
| **REBALANCE** | Adjust existing positions to target weights (no new initiations) |
| **NO ACTION** | Insufficient conditions — not actionable |

The Manager's BUY output is structured as:

```
- **Opportunity**: Ticker + one-sentence thesis
- **Macro Alignment**: How the Economist's regime view affects this decision
- **Your Decision**: BUY / SELL / HOLD / REBALANCE / NO ACTION
- **Rationale**: 2–4 paragraphs
- **Scenario Weighted View**: Which Associate scenario is most compelling and why
- **Position Sizing**: % of portfolio to allocate
- **Risk Level**: High / Medium / Low
- **Sell Conditions**: [ list of structured conditions — see Section 4 ]
- **Timeframe**: Expected holding period
- **Dissent/Disagreement**: Any views being overridden
```

For SELL decisions, the Manager specifies which position, how many shares, and the reason.

**Execution:** The Manager calls the `PaperTrader` Python API directly to execute trades.

**Output file:** `research/manager_decision_YYYY-MM-DD.md` (dated)

---

## 3. The Trading Engine (`trader/engine.py`)

### Core Class: `PaperTrader`

Manages a persistent simulated portfolio backed by `trader/data/portfolio.json`. Executes at live market prices via yfinance.

#### State Schema (`portfolio.json`)

```json
{
  "created_at": "2026-04-13T17:26:34.701095+00:00",
  "initial_capital": 100000.0,
  "cash": 100000.0,
  "positions": {},
  "transactions": [],
  "pending_sells": []
}
```

#### Position Object (within `positions`)

Each open position stores:

```json
{
  "symbol": "AAPL",
  "shares": 10,
  "avg_cost": 198.50,
  "total_cost": 1985.00,
  "buy_reason": "Strong fundamentals per Analyst — DCF shows 25% upside",
  "buy_date": "2026-04-13T15:25:07Z",
  "peak_price": 210.75,
  "sell_conditions": [
    {
      "condition_id": "cond_a1b2c3",
      "type": "price_target",
      "target": 240.00,
      "operator": "gte",
      "reason": "Analyst base case target $240",
      "created_at": "2026-04-13T15:25:07Z",
      "triggered": false,
      "triggered_at": null
    },
    {
      "condition_id": "cond_d4e5f6",
      "type": "pnl_pct",
      "target": -10.0,
      "operator": "lte",
      "reason": "Stop loss at -10% per risk policy",
      "created_at": "2026-04-13T15:25:07Z",
      "triggered": false,
      "triggered_at": null
    },
    {
      "condition_id": "cond_g7h8i9",
      "type": "fundamental",
      "metric": "trailingPE",
      "operator": "gt",
      "target": 30.0,
      "reason": "Sell if P/E rises above 30 — stretched valuation",
      "created_at": "2026-04-13T15:25:07Z",
      "triggered": false,
      "triggered_at": null
    }
  ]
}
```

### API Methods

```python
pt = PaperTrader()

# Trading
pt.buy(symbol, quantity, reason, sell_conditions=None, force=False)
pt.sell(symbol, quantity, reason, force=False)

# Portfolio inspection
pt.get_portfolio()          # Full state with live valuations + condition summaries
pt.get_position(symbol)      # Single position + its conditions
pt.get_transaction_history(limit=50)

# Sell condition monitoring
pt.check_sell_conditions()     # Evaluates all conditions, returns triggered/approaching alerts
pt.get_condition_status(symbol) # Per-position condition summary with current values + distance

# Lifecycle
pt.reset(initial_capital=None)
```

### Condition Types

| Type | Evaluated With | Extra yfinance call |
|---|---|---|
| `price_target` | Current price vs. target | No (already fetched in get_portfolio) |
| `pnl_pct` | (current_price − avg_cost) / avg_cost × 100 vs. target | No |
| `trailing_stop` | Peak price minus drawdown threshold vs. target | No (peak tracked per position) |
| `weight_pct` | Position market value / total portfolio value × 100 vs. target | No |
| `fundamental` | yfinance `ticker.info[metric]` vs. target | Yes (1 call/position) |

### Supported Fundamental Metrics (from yfinance `ticker.info`)

The Manager can write any of these as a `fundamental` condition:

| Metric | yfinance key | Description |
|---|---|---|
| Trailing P/E | `trailingPE` | Price / trailing 12-month EPS |
| Forward P/E | `forwardPE` | Price / forward 12-month EPS |
| PEG Ratio | `pegRatio` | Price / earnings growth |
| Price to Book | `priceToBook` | Price / book value per share |
| EV/EBITDA | `evToEbitda` | Enterprise value / EBITDA |
| Revenue Growth | `revenueGrowth` | YoY revenue growth rate |
| Earnings Growth | `earningsGrowth` | YoY earnings growth rate |
| EBITDA Margin | `ebitdaMargins` | EBITDA / revenue |
| Debt to Equity | `debtToEquity` | Total debt / equity |
| Current Ratio | `currentRatio` | Current assets / liabilities |
| Beta | `beta` | Market sensitivity |
| Analyst Target | `analystTargetPrice` | Wall Street consensus target |
| Recommendation | `recommendationKey` | buy / hold / sell / etc. |

---

## 4. Sell Conditions

When the Manager calls `pt.buy()`, it passes a structured `sell_conditions` list. Each condition is evaluated programmatically by `check_sell_conditions()` on every monitoring tick.

### Example: Full BUY call with Sell Conditions

```python
result = pt.buy(
    symbol="AAPL",
    quantity=10,
    reason="Strong fundamentals: DCF shows 25% upside, Analyst target $240, macro regime supportive",
    sell_conditions=[
        {
            "type": "price_target",
            "target": 240.00,
            "operator": "gte",
            "reason": "Analyst base case target $240"
        },
        {
            "type": "pnl_pct",
            "target": -10.0,
            "operator": "lte",
            "reason": "Stop loss at -10% per risk policy"
        },
        {
            "type": "fundamental",
            "metric": "trailingPE",
            "target": 30.0,
            "operator": "gt",
            "reason": "Sell if valuation gets stretched above P/E 30"
        }
    ]
)
```

### How Conditions Are Evaluated

`check_sell_conditions()` runs on every monitoring tick:

```
For each position:
  For each non-triggered condition:
    If condition.type == "price_target":
      hit = (current_price >= 240.00)   # operator: gte
    If condition.type == "pnl_pct":
      pnl_pct = (current_price - avg_cost) / avg_cost * 100
      hit = (pnl_pct <= -10.0)            # operator: lte
    If condition.type == "fundamental":
      value = yf.Ticker(symbol).info["trailingPE"]
      hit = (value > 30.0)               # operator: gt
    If hit:
      condition["triggered"] = True
      condition["triggered_at"] = <timestamp>
      alert → triggered_conditions list
```

### Execution on Trigger

When a condition is triggered:
1. `check_sell_conditions()` checks `is_market_open(symbol)` — if the market is closed, the sell is queued in `pending_sells` within `portfolio.json`
2. If the market is open, the engine executes `pt.sell(symbol, quantity=pos["shares"], reason=f"Triggered: {condition['reason']}")`
3. Peak price is updated whenever the current price exceeds the stored peak (used by `trailing_stop` conditions)
4. Pending sells are re-evaluated on every `check_sell_conditions()` call — any whose market is now open are executed automatically

### Condition Evaluation Logic (pseudo-code)

```python
OPERATORS = {
    "gte": lambda val, target: val >= target,
    "lte": lambda val, target: val <= target,
    "gt":  lambda val, target: val > target,
    "lt":  lambda val, target: val < target,
    "eq":  lambda val, target: val == target,
}

def _evaluate_condition(cond, current_value):
    op_fn = OPERATORS[cond["operator"]]
    return op_fn(current_value, cond["target"])
```

---

## 5. Portfolio Vision: The Monitoring Layer

The Manager has a real-time view of the current portfolio that surfaces:
1. Current live P&L per position
2. Which positions are approaching their sell conditions (within 5% of target)
3. Which conditions have been triggered
4. Portfolio-level context: total exposure, cash, weights

### `get_portfolio()` Extended Output

Each position in the portfolio output gains a `condition_summary`:

```python
{
  "symbol": "AAPL",
  "shares": 10,
  "avg_cost": 198.50,
  "current_price": 201.25,
  "market_value": 2012.50,
  "cost_basis": 1985.00,
  "unrealized_pnl": 27.50,
  "unrealized_pnl_pct": 1.39,
  "weight_pct": 2.01,
  "sell_conditions": [...],  // full condition list
  "condition_summary": {
    "total": 3,
    "triggered": 0,
    "approaching": 1,   // within 5% of firing
    "distant": 2
  },
  "approaching_conditions": [
    {
      "condition_id": "cond_a1b2c3",
      "type": "price_target",
      "target": 240.00,
      "current": 201.25,
      "distance_pct": 16.1,   // 201.25 is 16.1% below 240
      "severity": "moderate"
    }
  ]
}
```

### `check_sell_conditions()` Return Format

```python
{
  "checked_at": "2026-04-18T14:30:00Z",
  "positions_checked": 5,
  "conditions_triggered": [
    {
      "symbol": "AAPL",
      "condition_id": "cond_a1b2c3",
      "type": "price_target",
      "target": 240.00,
      "operator": "gte",
      "triggered_at": "2026-04-18T14:30:00Z",
      "sell_executed": false,    // true if market was open and sell ran
      "sell_error": null         // "market closed", etc.
    }
  ],
  "conditions_approaching": [
    {
      "symbol": "AAPL",
      "condition_id": "cond_d4e5f6",
      "type": "pnl_pct",
      "target": -10.0,
      "operator": "lte",
      "current_value": -9.2,
      "distance_pct": 8.0        // 8% away from -10% threshold
    }
  ]
}
```

---

## 6. CLI Extensions

Three new commands for `trader/cli.py`:

### `python -m trader monitor`
Runs `check_sell_conditions()` and prints a formatted table of:
- All triggered conditions with symbol, condition type, target, triggered_at
- All approaching conditions with symbol, type, current value, distance from target
- An overall portfolio health summary

### `python -m trader position <SYMBOL>`
Detailed single-position view:
- Shares, avg_cost, current_price, unrealized P&L
- Full sell conditions table with status per condition
- "Approaching" indicators with % distance to target
- Buy reason and buy date

### `python -m trader add-condition <SYMBOL> --type <TYPE> --target <VALUE> --operator <OP> --reason <TEXT>`
Manually add a sell condition to an existing position. Useful for a human operator to add a stop-loss or price target after the fact.

---

## 7. The Full Operational Loop

The fund runs on two interleaved loops: the **research pipeline** (agent-driven, once per trading day) and the **monitoring loop** (programmatic, every N minutes).

### Loop A: Daily Research Pipeline (once per trading day)

```
[ECONOMIST]  → Macro briefing written to research/economist_briefing_YYYY-MM-DD.md
      ↓
[RESEARCHER] → Opportunities written to research/researcher_daily_YYYY-MM-DD.md
      ↓
[ANALYST]    → For each approved opportunity: pulls yfinance data → analyst_<ticker>.json
      ↓
[ASSOCIATES] → 3 independent scenarios → associate_<TYPE>_YYYY-MM-DD.md (1 base, 1 bear, 1 bull)
      ↓
[MANAGER]    → Reads all output → decision written to research/manager_decision_YYYY-MM-DD.md
              → If BUY: calls pt.buy() with sell_conditions list
              → If SELL: calls pt.sell() with reason
              → If REBALANCE: issues multiple buy/sell calls to reach target weights
              → If HOLD/NO ACTION: logs decision, no trade
```

### Loop B: Continuous Condition Monitoring (every 15 minutes, cron-scheduled)

```
[SYSTEM]     → pt.check_sell_conditions()
              → For each triggered condition:
                  → If market open: execute pt.sell() immediately
                  → If market closed: queue sell, execute at next market open
              → Log all triggered/approaching conditions
              → Expose via: python -m trader monitor
```

### Loop C: Manager Review Cycle (optional, human-in-the-loop)

```
[MANAGER]    → python -m trader portfolio  → sees full live portfolio
              → python -m trader monitor     → sees all conditions status
              → python -m trader position AAPL  → deep-dives a specific position
              → Can issue manual SELL: python -m trader sell AAPL 5 --reason "..."
```

### Market Hours Enforcement in Monitoring Loop

Before executing an auto-sell, `check_sell_conditions()` checks `is_market_open(symbol)`:

| Ticker | Calendar |
|---|---|
| US stocks | NYSE (Mon–Fri 9:30–16:00 ET) |
| UK stocks | LSE (Mon–Fri 8:00–16:30 GMT) |
| German stocks | XETR (Mon–Fri 9:00–17:30 CET) |
| Commodities | CME Globex (Sun–Fri, varies) |

If the market is closed, the sell is written to `pending_sells` in `portfolio.json` and fires at the next market open check.

---

## 8. Tool & Skill Inventory

| Tool / Skill | File | Used By | What It Does |
|---|---|---|---|
| Sonar (Perplexity) | Perplexity API | Economist | Reasoning model with native web search — reads macro sources directly |
| Yahoo Finance scraper | `scraper/yfinance_scraper.py` | Researcher | Playwright-based scraper collecting headlines + full articles from Yahoo Finance homepage |
| Pull market data | `skills/analyst_market_data.md` | Analyst | Runs `analyst/market_data.py` to pull yfinance data in modules |
| Financial models library | `analyst/models.py` | Analyst | Pre-built financial calculation functions: DCF, projections, technicals, peer multiples, CAPM, DDM |
| Code interpreter | `run_python` tool | Analyst | Sandboxed Python execution for custom/one-off analysis not covered by standard models |
| Paper trading | `skills/paper_trading.md` | Manager | Python API for buy/sell/get_portfolio |

---

## 9. Data Files

| File | Purpose | Updated |
|---|---|---|
| `trader/data/portfolio.json` | Portfolio state with positions + sell conditions | Every trade |
| `research/economist_briefing_YYYY-MM-DD.md` | Daily macro briefing | Daily |
| `research/researcher_daily_YYYY-MM-DD.md` | Daily opportunity scan | Daily |
| `research/analyst_<TICKER>.json` | Analyst output for a specific ticker | Per analysis |
| `research/associate_<TYPE>_YYYY-MM-DD.md` | Associate scenario output | Per analysis |
| `research/manager_decision_YYYY-MM-DD.md` | Manager's daily decision record | Daily |

---

## 10. Files and Components

| File | Purpose |
|---|---|
| `analyst/models.py` | Financial calculation library: `dcf_valuation()`, `projected_income_statement()`, `technical_summary()`, `peer_valuation_multiples()`, `scenario_model()`, `capm()`, `dividend_discount_model()`, `compound_growth_rate()` |
| `analyst/__init__.py` | Package exports: market data tools, model functions, `run_python` |
| `analyst/interpreter.py` | Sandboxed code interpreter: `run_python(code, imports)` with pre-loaded `pull_ticker_data`, `snapshot`, `financials`, `history`, `analysis`, `math`, `statistics` |
| `prompts/02_analyst.md` | Analyst system prompt with full "Available Calculation Tools" section: Layer 1 model calling conventions, Layer 2 `run_python` usage and examples, Layer 3 prompt examples, data-pull workflow |
| `trader/engine.py` | Trading engine: `buy()`, `sell()`, `get_portfolio()`, `get_position()`; extended with `sell_conditions` on positions, `check_sell_conditions()`, `_evaluate_condition()`, `_check_fundamental()`, peak price tracking |
| `trader/data/portfolio.json` | Portfolio state: positions with cost basis, `sell_conditions[]`, `buy_reason`, `buy_date`, `peak_price` |
| `trader/cli.py` | CLI: `monitor`, `position`, `add-condition` commands; portfolio/positions commands surface condition status |
| `prompts/04_manager.md` | Manager system prompt: structured sell conditions output format |
| `skills/paper_trading.md` | Paper trading skill: `buy()` with `sell_conditions` parameter, `check_sell_conditions()` usage |

---

## 11. yfinance Fundamental Metrics Mapping

When the Manager writes a `fundamental` sell condition, it uses a metric key that maps directly to `yfinance.Ticker.info`. This allows `check_sell_conditions()` to evaluate it with one extra API call per position:

```
trailingPE    → ticker.info["trailingPE"]
forwardPE    → ticker.info["forwardPE"]
pegRatio     → ticker.info["pegRatio"]
priceToBook  → ticker.info["priceToBook"]
evToEbitda   → ticker.info["evToEbitda"]
revenueGrowth       → ticker.info["revenueGrowth"]
earningsGrowth      → ticker.info["earningsGrowth"]
ebitdaMargins       → ticker.info["ebitdaMargins"]
debtToEquity        → ticker.info["debtToEquity"]
currentRatio        → ticker.info["currentRatio"]
beta                → ticker.info["beta"]
analystTargetPrice  → ticker.info["analystTargetPrice"]
recommendationKey  → ticker.info["recommendationKey"]
```

This means the Manager can write conditions like:
- `"Sell if trailingPE > 30"` — stretched valuation
- `"Sell if revenueGrowth < 0.02"` — earnings growth stalls
- `"Sell if analystTargetPrice < currentPrice"` — analyst downgrades consensus

All evaluated programmatically, all automatic.

---

## 12. Scenario: A Complete Trade Lifecycle

1. **Day 1, 08:00 ET — Economist runs** → Macro briefing: "Risk-On regime, tech sector tailwind" → `research/economist_briefing_YYYY-MM-DD.md`
2. **Day 1, 08:30 ET — Researcher runs** → Surfaces AAPL opportunity from earnings beat article (score: 8) → `research/researcher_daily_YYYY-MM-DD.md`
3. **Day 1, 09:00 ET — Analyst runs** → Pulls yfinance data for AAPL (snapshot + financials + history + analysis modules), calls `dcf_valuation()` and `technical_summary()` from `analyst/models.py`, runs peer comparison → "Outlook: Bullish, proceed to Associates"
4. **Day 1, 10:00 ET — Associates run** → 3 scenarios built; base case AAPL $240 in 6 months
5. **Day 1, 11:00 ET — Manager runs** → "Decision: BUY AAPL, 5% portfolio, Sell conditions: [price_target $240 gte, pnl_pct -10% lte, fundamental trailingPE > 30 gt]"

```python
pt.buy(symbol="AAPL", quantity=25,
       reason="Bullish base case $240 in 6 months, P/E 22 reasonable, macro tailwind",
       sell_conditions=[
           {"type": "price_target", "target": 240.00, "operator": "gte", "reason": "Base case target"},
           {"type": "pnl_pct", "target": -10.0, "operator": "lte", "reason": "Stop loss"},
           {"type": "fundamental", "metric": "trailingPE", "target": 30.0, "operator": "gt", "reason": "Stretched valuation"}
       ])
```

6. **Day 1, 11:05 ET — MONITOR LOOP kicks off** → 15-minute cron runs `check_sell_conditions()` continuously
7. **Day 3, 14:45 ET — AAPL hits $240** → `check_sell_conditions()` sees price >= 240.00 (gte), condition `cond_a1b2c3` triggered → `pt.sell()` executed automatically → position closed, $1,037.50 gain recorded
8. **Day 3, 14:46 ET — MONITOR LOOP logs**: condition `cond_a1b2c3` (price_target $240 gte) triggered and executed

---

*End of PLAN2.md v2*