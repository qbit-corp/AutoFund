# Skill: Execute Paper Trades via the Trading Engine

## Purpose
You have access to a paper trading engine that lets you execute simulated BUY and SELL orders at **live market prices** via `yfinance`. Use this tool to act on the investment decisions you produce at the end of the research pipeline. The engine manages a persistent portfolio with a cash balance, tracks positions and P&L, and enforces market-hours restrictions.

## When to Use This Skill
Use this skill whenever your final decision output includes:
- **BUY** — Open a new position or add to an existing one.
- **SELL** — Close or reduce an existing position.
- **REBALANCE** — Adjust existing positions (issue individual BUY and SELL orders to reach target weights).

Do **not** use this skill for HOLD or NO ACTION decisions — there is nothing to execute.

## Tradeable Universe
The engine only allows trading in the following asset classes:

| Category | Examples | Ticker format |
|---|---|---|
| US stocks | AAPL, MSFT, GOOGL, JPM, JNJ | Plain ticker |
| European stocks | SHEL.L (London), SAP.DE (Xetra), ASML.AS (Amsterdam), TTE.PA (Paris) | Ticker + exchange suffix |
| Commodity futures | GC=F (Gold), SI=F (Silver), CL=F (WTI Oil), BZ=F (Brent Oil), NG=F (Natural Gas), HG=F (Copper), PL=F (Platinum) | Ticker + `=F` |

**The engine will reject:**
- Asian equities (Tokyo, Hong Kong, Shanghai, etc.)
- Options and derivatives
- Cryptocurrencies
- Mutual funds
- OTC / private equity

If you attempt a trade with an invalid ticker, you will receive a clear error message explaining why it was rejected. Do not retry with the same ticker.

## How to Use It

### Python API (Primary Method)
The trading engine is a Python class you invoke programmatically. All commands are run from the project root (`AutoFund/`).

#### Initialization
```python
from trader import PaperTrader

pt = PaperTrader()
```

This loads the existing portfolio from `trader/data/portfolio.json`, or creates a new one with $100,000 starting capital if none exists.

#### Buying
```python
result = pt.buy(
    symbol="AAPL",
    quantity=10,
    reason="Strong fundamentals: FCF yield 4.2%, consensus underweight, golden cross on daily",
    sell_conditions=[
        {"type": "price_target", "target": 240.00, "operator": "gte", "reason": "Analyst base case target $240"},
        {"type": "pnl_pct", "target": -10.0, "operator": "lte", "reason": "Stop loss at -10%"},
        {"type": "fundamental", "metric": "trailingPE", "target": 30.0, "operator": "gt", "reason": "Sell if P/E > 30"}
    ]
)
```

**Parameters:**
- `symbol` (str, required) — Ticker symbol. Case-insensitive.
- `quantity` (int, required) — Number of shares or contracts. Must be > 0.
- `reason` (str, recommended) — Your rationale. This is recorded in the transaction log. **Always provide a reason** so the trade history is auditable.
- `sell_conditions` (list[dict], recommended) — Structured sell conditions that will be monitored automatically. Each condition is a dict with: `type`, `target`, `operator`, `reason`, and optionally `metric` (for `fundamental` type).

**Condition types:**
| Type | What it checks | Example |
|---|---|---|
| `price_target` | Current price vs. target | Price >= $240 |
| `pnl_pct` | Unrealized P&L % vs. target | P&L <= -10% |
| `trailing_stop` | Drawdown from peak vs. target | Peak drawdown >= 5% |
| `weight_pct` | Portfolio weight % vs. target | Weight >= 15% |
| `fundamental` | yfinance metric vs. target | trailingPE > 30 |

**Returns:** A dict with the executed transaction details:
```json
{
    "id": "txn_8bceb0a1",
    "timestamp": "2026-04-13T15:25:07+00:00",
    "type": "BUY",
    "symbol": "AAPL",
    "shares": 10,
    "price": 198.50,
    "total": 1985.00,
    "reason": "Strong fundamentals: FCF yield 4.2%, consensus underweight, golden cross on daily"
}
```

**Raises `TradeError` if:**
- The ticker is not in the tradeable universe.
- The relevant market is currently closed.
- You don't have enough cash for the order.

#### Sell Condition Monitoring
Once a position has sell conditions, they are evaluated automatically by the monitoring loop (`python monitor.py --loop`). When a condition triggers:
- If the market is open → the sell executes immediately
- If the market is closed → the sell is queued and executes at next market open

Check condition status at any time:
```python
status = pt.check_sell_conditions()     # All positions
status = pt.get_condition_status("AAPL")  # Per position
```

#### Selling
```python
result = pt.sell(
    symbol="AAPL",
    quantity=5,
    reason="Reducing position after hitting price target ($210)"
)
```

**Parameters:** Same as `buy()`.

**Raises `TradeError` if:**
- You don't hold any shares of the ticker.
- You try to sell more shares than you own.
- The relevant market is currently closed.

#### Checking the Portfolio (Before Trading)
Always check the portfolio state before making trade decisions:

```python
portfolio = pt.get_portfolio()
```

**Returns:**
```json
{
    "cash": 87913.50,
    "initial_capital": 100000.00,
    "positions": [
        {
            "symbol": "AAPL",
            "shares": 10,
            "avg_cost": 198.50,
            "current_price": 201.25,
            "market_value": 2012.50,
            "cost_basis": 1985.00,
            "unrealized_pnl": 27.50,
            "unrealized_pnl_pct": 1.39,
            "weight_pct": 2.01
        }
    ],
    "total_invested": 1985.00,
    "total_market_value": 2012.50,
    "total_value": 89926.00,
    "total_pnl": -74.00,
    "total_return_pct": -0.07,
    "transaction_count": 1
}
```

Use this data to inform:
- **Position sizing**: Check `cash` to know how much buying power you have.
- **Concentration risk**: Check `weight_pct` to avoid over-allocating to one name.
- **Existing exposure**: Check whether you already hold the stock before buying more.

#### Checking a Single Position
```python
position = pt.get_position("AAPL")
# Returns None if not held
```

#### Reviewing Transaction History
```python
history = pt.get_transaction_history(limit=20)
# Returns a list of the 20 most recent transactions, newest first.
```

### CLI (Inspection Only)
The CLI is for human operators to inspect the portfolio. You do not need the CLI for automated trading, but you may reference these commands in your output so a human can verify your trades:

```bash
python -m trader portfolio       # Full dashboard with live P&L
python -m trader positions       # Positions table only
python -m trader history         # Transaction log
python -m trader status          # Which exchanges are open right now
```

## Market Hours Enforcement
The engine checks whether the relevant exchange is open before executing any trade:

| Ticker type | Exchange calendar checked |
|---|---|
| US stocks (AAPL, MSFT) | NYSE (Mon-Fri, 9:30-16:00 ET) |
| UK stocks (.L suffix) | LSE (Mon-Fri, 8:00-16:30 GMT) |
| German stocks (.DE suffix) | XETR (Mon-Fri, 9:00-17:30 CET) |
| Dutch/French stocks (.AS, .PA) | Euronext (Mon-Fri, 9:00-17:30 CET) |
| Commodity futures (GC=F, CL=F) | CME Globex (Sun-Fri, varies by product) |

If the market is closed, the trade will be rejected with a message telling you when the market next opens. **Do not attempt to trade outside market hours.** Instead, note the pending trade in your output and indicate it should be executed when the market opens.

## Position Sizing Guidelines
When deciding how many shares to buy, consider:

1. **Check available cash first**: `portfolio["cash"]`
2. **Respect concentration limits**: A single position should typically not exceed 10-15% of total portfolio value unless there is extremely high conviction.
3. **Calculate order size**:
   ```python
   # Example: allocate 5% of portfolio to AAPL
   portfolio = pt.get_portfolio()
   target_allocation = 0.05  # 5%
   target_value = portfolio["total_value"] * target_allocation
   price = 198.50  # from your market data
   shares_to_buy = int(target_value // price)
   ```
4. **Keep a cash reserve**: Avoid going below 10-20% cash to maintain flexibility for future opportunities.

## Executing a REBALANCE Decision
A rebalance is not a single operation — execute it as a sequence of sells followed by buys:

1. Call `pt.get_portfolio()` to see current allocations.
2. Determine which positions are overweight (sell some) and which are underweight (buy more).
3. Execute sells first to free up cash.
4. Then execute buys with the available cash.
5. Provide a clear reason on each trade referencing the rebalance rationale.

## Error Handling
All trade errors raise `TradeError` with a descriptive message. When you encounter an error:

| Error | What to do |
|---|---|
| Ticker rejected | Do not retry. The asset is outside the allowed universe. |
| Market closed | Note the trade as pending. Specify the intended order in your output. |
| Insufficient cash | Reduce the order size, or sell another position first to free cash. |
| Insufficient shares | Check your actual position size with `get_position()` and adjust the sell quantity. |
| Price unavailable | The ticker may be delisted or data is temporarily unavailable. Skip this trade and note the issue. |

## Important Rules
- **Always provide a reason** for every trade. The reason should reference the analysis that supports the decision (e.g., which scenario, what the Analyst found, what the macro regime implies).
- **Always check the portfolio before trading** to avoid insufficient-cash or over-concentration errors.
- **Never fabricate trade confirmations.** If a trade fails, report the failure honestly.
- **All trades execute at the current market price** (market orders only). There are no limit orders or stop-losses.
- **The portfolio persists across sessions.** Every trade you make permanently alters the portfolio state in `trader/data/portfolio.json`.
