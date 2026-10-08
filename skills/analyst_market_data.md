# Skill: Pull Market Data from Yahoo Finance

## Purpose
You have access to a Python tool that retrieves structured financial data from Yahoo Finance via the `yfinance` library. Use this tool to obtain all the market data you need for your analysis — valuation metrics, price history, financial statements, technical indicators, analyst consensus, and holder information.

## When to Use This Skill
Use this skill whenever you need to:
- **Pull current market data** (price, P/E, EPS, market cap, 52-week range, volume, etc.) for an asset flagged by the Researcher.
- **Get price history** for technical analysis (trend identification, support/resistance, momentum).
- **Retrieve financial statements** (income statement, balance sheet, cash flow) for DCF modelling or fundamental analysis.
- **Check analyst consensus** (price targets, recommendations, earnings estimates) for relative valuation and quality assessment.
- **Compute technical indicators** (SMA, EMA, RSI, MACD, Bollinger Bands, ATR) automatically.
- **Review institutional and insider holdings** to gauge conviction and sentiment.

## How to Use It

### Command Format
Run the script from the `analyst/` directory:

```bash
python analyst/market_data.py <TICKER(S)> [OPTIONS]
```

### Basic Examples

```bash
# Full data pull for a single stock
python analyst/market_data.py AAPL

# Multiple tickers at once
python analyst/market_data.py AAPL MSFT GOOGL

# Print to stdout (useful when you need to read the data directly)
python analyst/market_data.py AAPL --stdout

# Specific output file
python analyst/market_data.py AAPL -o data/aapl_analysis.json
```

### Selecting Specific Data Modules
You don't always need everything. Use `--modules` to pull only what you need:

```bash
# Only valuation snapshot and technicals (quick check)
python analyst/market_data.py AAPL --modules snapshot technicals

# Only financials for DCF modelling
python analyst/market_data.py AAPL --modules financials

# Snapshot + analyst consensus for relative valuation
python analyst/market_data.py AAPL --modules snapshot analysis
```

**Available modules:**

| Module | What it contains | Use it for |
|--------|-----------------|------------|
| `snapshot` | Price, valuation ratios, margins, debt metrics, dividends, beta, analyst targets | Quick overview, relative valuation, quality checks |
| `history` | OHLCV data + return statistics (total return, volatility, max drawdown) | Technical analysis, trend identification |
| `financials` | Income statement, balance sheet, cash flow (annual + quarterly) | DCF models, fundamental analysis |
| `analysis` | Analyst price targets, recommendations, earnings/revenue estimates, growth estimates, EPS trend, upgrades/downgrades | Relative valuation, consensus checks |
| `technicals` | SMA 20/50/200, EMA 12/26, RSI 14, MACD, Bollinger Bands, ATR + signal interpretation | Technical analysis, entry/exit timing |
| `holders` | Major holders, institutional holders, mutual fund holders, insider transactions | Insider sentiment, institutional conviction |

### Customising History Period and Interval

```bash
# 2 years of weekly data (for longer-term trend analysis)
python analyst/market_data.py AAPL --period 2y --interval 1wk

# 6 months of daily data
python analyst/market_data.py AAPL --period 6mo --interval 1d

# Maximum available history
python analyst/market_data.py AAPL --period max --interval 1d
```

**Period options:** `1d`, `5d`, `1mo`, `3mo`, `6mo`, `1y`, `2y`, `5y`, `10y`, `ytd`, `max`
**Interval options:** `1m`, `2m`, `5m`, `15m`, `30m`, `60m`, `90m`, `1h`, `1d`, `5d`, `1wk`, `1mo`, `3mo`

> **Note:** Intraday intervals (< 1d) are only available for recent data (last 30-60 days).

## Output Format
The tool outputs a JSON file with this structure:

```json
{
  "generated_at": "2026-04-11T16:00:00+00:00",
  "tool": "AutoFund Analyst Market Data Tool",
  "parameters": {
    "tickers": ["AAPL"],
    "modules": ["snapshot", "history", "financials", "analysis", "technicals", "holders"],
    "period": "1y",
    "interval": "1d"
  },
  "data": {
    "symbol": "AAPL",
    "snapshot": { ... },
    "history": { "stats": { ... }, "data": [ ... ] },
    "financials": { ... },
    "analysis": { ... },
    "technicals": { "latest": { ... } },
    "holders": { ... }
  }
}
```

When multiple tickers are requested, `data` becomes a list of these objects.

## Mapping Data to Your Analysis Types

### For DCF Models
Use modules: `snapshot`, `financials`
- `snapshot.free_cashflow`, `snapshot.operating_cashflow` → starting FCF
- `financials.income_statement_annual` → revenue and earnings trajectory
- `financials.cash_flow_annual` → historical FCF, capex
- `financials.balance_sheet_annual` → debt, cash, shares outstanding
- `snapshot.beta` → WACC calculation
- `analysis.growth_estimates` → terminal growth rate input

### For Technical Analysis
Use modules: `history`, `technicals`
- `technicals.latest` → pre-computed SMA, EMA, RSI, MACD, Bollinger Bands, ATR
- `technicals.latest.signals` → plain-language interpretation of current signals
- `history.data` → raw OHLCV if you need to perform additional analysis
- `history.stats` → summary statistics (return, volatility, drawdown)

### For Relative Valuation
Use modules: `snapshot`, `analysis`
- `snapshot.trailing_pe`, `snapshot.forward_pe`, `snapshot.peg_ratio` → compare vs peers
- `snapshot.price_to_book`, `snapshot.ev_to_ebitda`, `snapshot.ev_to_revenue` → multiples
- `analysis.analyst_price_targets` → wall street consensus
- `analysis.recommendations_summary` → buy/hold/sell distribution
- `analysis.upgrades_downgrades_recent` → recent sentiment shifts

### For Scenario Stress-Testing
Use modules: `snapshot`, `history`, `analysis`
- `history.stats.annualised_volatility_pct` → volatility assumption
- `history.stats.max_drawdown_pct` → worst-case historical reference
- `snapshot.beta` → market sensitivity
- `analysis.earnings_estimate` → consensus expectations to stress
- `analysis.growth_estimates` → growth assumptions to vary

### For Quality Assessment
Use modules: `snapshot`, `analysis`, `holders`
- `snapshot.profit_margins`, `snapshot.return_on_equity`, `snapshot.debt_to_equity` → financial quality
- `snapshot.short_percent_of_float`, `snapshot.short_ratio` → short interest (contrarian signal)
- `holders.institutional_holders` → smart money positioning
- `holders.insider_transactions_recent` → insider conviction
- `analysis.earnings_history` → beat/miss track record

## Error Handling
- If a module returns `null` for a field, that data is unavailable from Yahoo Finance for this ticker. Proceed with what you have and note the limitation.
- If the entire tool fails, log the error and note in your output that market data could not be retrieved. Do not fabricate data.
- Commodity tickers (e.g. `GC=F` for gold, `CL=F` for crude oil) and ETFs work but may not have all modules (e.g. no `financials` for commodities).

## Important Notes
- The data comes from Yahoo Finance via the `yfinance` library. It is unofficial and should be cross-referenced when possible.
- Always check the `generated_at` timestamp to ensure data freshness.
- For non-US stocks, append the exchange suffix (e.g. `BARC.L` for Barclays on London Stock Exchange).
