"""
AutoFund — Analyst Market Data Tool
====================================
Pulls structured market data from Yahoo Finance via the `yfinance` library.
Designed to be called by the Analyst agent (or invoked from the CLI) to
retrieve everything needed for DCF models, technical analysis, relative
valuation, and scenario stress-testing.

Usage:
    # Full data pull for a single ticker
    python market_data.py AAPL

    # Multiple tickers
    python market_data.py AAPL MSFT GOOGL

    # Only specific modules
    python market_data.py AAPL --modules snapshot history technicals

    # Custom history period / interval
    python market_data.py AAPL --period 2y --interval 1wk

    # Write to a specific file
    python market_data.py AAPL -o aapl_analysis.json

    # Pretty-print to stdout instead of writing a file
    python market_data.py AAPL --stdout
"""

import argparse
import json
import logging
import sys
import warnings
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# ── logging ────────────────────────────────────────────────────────────────────
log = logging.getLogger("analyst-data")

# ── Available data modules ─────────────────────────────────────────────────────
ALL_MODULES = [
    "snapshot",     # key valuation & identity metrics from .info
    "history",      # OHLCV price history
    "financials",   # income statement, balance sheet, cash flow
    "analysis",     # analyst targets, recommendations, earnings estimates
    "technicals",   # computed technical indicators (SMA, EMA, RSI, MACD, Bollinger)
    "holders",      # institutional, mutual-fund, and insider holders
]


# ── Helpers ────────────────────────────────────────────────────────────────────
def _safe(val):
    """Make a value JSON-serialisable (handles np/pd types and NaN)."""
    if val is None:
        return None
    if pd.api.types.is_scalar(val) and pd.isna(val):
        return None
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        v = float(val)
        return None if np.isnan(v) else v
    if isinstance(val, (np.bool_,)):
        return bool(val)
    if isinstance(val, (pd.Timestamp, datetime)):
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, np.ndarray):
        return [_safe(x) for x in val.tolist()]
    if isinstance(val, float):
        return None if np.isnan(val) else val
    if isinstance(val, list):
        return [_safe(x) for x in val]
    if isinstance(val, dict):
        return {str(k): _safe(v) for k, v in val.items()}
    if isinstance(val, tuple):
        return tuple(_safe(x) for x in val)
    return val


def _df_to_records(df: pd.DataFrame | None) -> list[dict] | None:
    """Convert a pandas DataFrame to a list of dicts, serialising cleanly."""
    if df is None or df.empty:
        return None
    df = df.copy()
    # If the index is a DatetimeIndex or has dates, include as a column
    if isinstance(df.index, pd.DatetimeIndex):
        df.index = df.index.strftime("%Y-%m-%d")
    df = df.reset_index()
    records = df.to_dict(orient="records")
    return [{k: _safe(v) for k, v in row.items()} for row in records]


def _df_to_dict_by_date(df: pd.DataFrame | None) -> dict | None:
    """Financial statements: columns are dates, rows are line items."""
    if df is None or df.empty:
        return None
    result = {}
    for col in df.columns:
        date_key = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)
        result[date_key] = {
            str(idx): _safe(df.at[idx, col]) for idx in df.index
        }
    return result


# ── Data extraction functions ──────────────────────────────────────────────────
def get_snapshot(ticker: yf.Ticker) -> dict:
    """
    Key identity + valuation metrics from .info.
    These are the numbers the Analyst cites in the 'Market Data Retrieved' section.
    """
    info = ticker.info or {}

    # Select the fields most relevant to the Analyst's workflow
    fields = {
        # Identity
        "symbol": "symbol",
        "name": "longName",
        "sector": "sector",
        "industry": "industry",
        "exchange": "exchange",
        "currency": "currency",
        "country": "country",
        "website": "website",
        "description": "longBusinessSummary",
        # Price
        "current_price": "currentPrice",
        "previous_close": "previousClose",
        "open": "open",
        "day_low": "dayLow",
        "day_high": "dayHigh",
        "52_week_low": "fiftyTwoWeekLow",
        "52_week_high": "fiftyTwoWeekHigh",
        "50_day_average": "fiftyDayAverage",
        "200_day_average": "twoHundredDayAverage",
        # Volume
        "volume": "volume",
        "average_volume": "averageVolume",
        "average_volume_10d": "averageDailyVolume10Day",
        # Valuation
        "market_cap": "marketCap",
        "enterprise_value": "enterpriseValue",
        "trailing_pe": "trailingPE",
        "forward_pe": "forwardPE",
        "peg_ratio": "pegRatio",
        "price_to_book": "priceToBook",
        "price_to_sales": "priceToSalesTrailing12Months",
        "ev_to_revenue": "enterpriseToRevenue",
        "ev_to_ebitda": "enterpriseToEbitda",
        # Earnings & profitability
        "trailing_eps": "trailingEps",
        "forward_eps": "forwardEps",
        "revenue": "totalRevenue",
        "revenue_per_share": "revenuePerShare",
        "gross_margins": "grossMargins",
        "operating_margins": "operatingMargins",
        "profit_margins": "profitMargins",
        "ebitda": "ebitda",
        "return_on_assets": "returnOnAssets",
        "return_on_equity": "returnOnEquity",
        # Dividends
        "dividend_rate": "dividendRate",
        "dividend_yield": "dividendYield",
        "payout_ratio": "payoutRatio",
        "ex_dividend_date": "exDividendDate",
        # Balance sheet highlights
        "total_cash": "totalCash",
        "total_debt": "totalDebt",
        "debt_to_equity": "debtToEquity",
        "current_ratio": "currentRatio",
        "free_cashflow": "freeCashflow",
        "operating_cashflow": "operatingCashflow",
        # Shares
        "shares_outstanding": "sharesOutstanding",
        "float_shares": "floatShares",
        "shares_short": "sharesShort",
        "short_ratio": "shortRatio",
        "short_percent_of_float": "shortPercentOfFloat",
        # Analyst consensus
        "target_high_price": "targetHighPrice",
        "target_low_price": "targetLowPrice",
        "target_mean_price": "targetMeanPrice",
        "target_median_price": "targetMedianPrice",
        "recommendation_key": "recommendationKey",
        "number_of_analyst_opinions": "numberOfAnalystOpinions",
        # Beta
        "beta": "beta",
    }

    snapshot = {}
    for out_key, info_key in fields.items():
        val = info.get(info_key)
        snapshot[out_key] = _safe(val)

    return snapshot


def get_history(ticker: yf.Ticker, period: str = "1y", interval: str = "1d") -> dict:
    """
    OHLCV price history — the raw material for technical analysis.
    Also computes basic return statistics.
    """
    hist = ticker.history(period=period, interval=interval)
    if hist.empty:
        log.warning("No price history returned for %s.", ticker.ticker)
        return {"period": period, "interval": interval, "data": None, "stats": None}

    # Return statistics
    close = hist["Close"]
    returns = close.pct_change().dropna()

    stats = {
        "period": period,
        "interval": interval,
        "data_points": len(hist),
        "start_date": hist.index[0].strftime("%Y-%m-%d"),
        "end_date": hist.index[-1].strftime("%Y-%m-%d"),
        "start_price": _safe(close.iloc[0]),
        "end_price": _safe(close.iloc[-1]),
        "total_return_pct": _safe(((close.iloc[-1] / close.iloc[0]) - 1) * 100),
        "high": _safe(close.max()),
        "low": _safe(close.min()),
        "mean": _safe(close.mean()),
        "std_dev": _safe(close.std()),
        "avg_daily_return_pct": _safe(returns.mean() * 100),
        "daily_volatility_pct": _safe(returns.std() * 100),
        "annualised_volatility_pct": _safe(returns.std() * (252 ** 0.5) * 100),
        "max_drawdown_pct": _safe(
            ((close / close.cummax()) - 1).min() * 100
        ),
        "avg_volume": _safe(hist["Volume"].mean()),
    }

    return {
        "stats": stats,
        "data": _df_to_records(hist),
    }


def get_financials(ticker: yf.Ticker) -> dict:
    """
    Income statement, balance sheet, and cash flow — annual and quarterly.
    This is the core input for DCF modelling.
    """
    # ticker.earnings is deprecated in yfinance >= 1.2; use income_stmt instead
    earnings = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            earnings = _df_to_records(ticker.earnings)
        except Exception:
            pass

    return {
        "income_statement_annual": _df_to_dict_by_date(ticker.income_stmt),
        "income_statement_quarterly": _df_to_dict_by_date(ticker.quarterly_income_stmt),
        "balance_sheet_annual": _df_to_dict_by_date(ticker.balance_sheet),
        "balance_sheet_quarterly": _df_to_dict_by_date(ticker.quarterly_balance_sheet),
        "cash_flow_annual": _df_to_dict_by_date(ticker.cashflow),
        "cash_flow_quarterly": _df_to_dict_by_date(ticker.quarterly_cashflow),
        "earnings": earnings,
    }


def get_analysis(ticker: yf.Ticker) -> dict:
    """
    Analyst consensus, price targets, earnings estimates, growth estimates,
    recent upgrades/downgrades — used for relative valuation and quality checks.
    """
    # Analyst price targets
    try:
        targets = ticker.analyst_price_targets
        if isinstance(targets, dict):
            analyst_targets = {k: _safe(v) for k, v in targets.items()}
        else:
            analyst_targets = None
    except Exception:
        analyst_targets = None

    # Recommendations summary
    try:
        rec_summary = _df_to_records(ticker.recommendations_summary)
    except Exception:
        rec_summary = None

    # Recent upgrades/downgrades
    try:
        upgrades = ticker.upgrades_downgrades
        if upgrades is not None and not upgrades.empty:
            # Only keep last 20 for brevity
            upgrades_recent = _df_to_records(upgrades.head(20))
        else:
            upgrades_recent = None
    except Exception:
        upgrades_recent = None

    # Earnings estimates
    try:
        earnings_est = _df_to_records(ticker.earnings_estimate)
    except Exception:
        earnings_est = None

    # Revenue estimates
    try:
        revenue_est = _df_to_records(ticker.revenue_estimate)
    except Exception:
        revenue_est = None

    # EPS trend
    try:
        eps_trend = _df_to_records(ticker.eps_trend)
    except Exception:
        eps_trend = None

    # Growth estimates
    try:
        growth_est = _df_to_records(ticker.growth_estimates)
    except Exception:
        growth_est = None

    # Earnings history (actual vs estimate)
    try:
        earnings_hist = _df_to_records(ticker.earnings_history)
    except Exception:
        earnings_hist = None

    # Upcoming calendar events (earnings dates, etc.)
    try:
        cal = ticker.calendar
        if isinstance(cal, dict):
            calendar = {k: _safe(v) for k, v in cal.items()}
        elif isinstance(cal, pd.DataFrame):
            calendar = _df_to_records(cal)
        else:
            calendar = None
    except Exception:
        calendar = None

    return {
        "analyst_price_targets": analyst_targets,
        "recommendations_summary": rec_summary,
        "upgrades_downgrades_recent": upgrades_recent,
        "earnings_estimate": earnings_est,
        "revenue_estimate": revenue_est,
        "eps_trend": eps_trend,
        "growth_estimates": growth_est,
        "earnings_history": earnings_hist,
        "calendar": calendar,
    }


def get_technicals(ticker: yf.Ticker, period: str = "1y", interval: str = "1d") -> dict:
    """
    Compute technical indicators on price history:
    SMA 20/50/200, EMA 12/26, RSI 14, MACD, Bollinger Bands, ATR.
    """
    hist = ticker.history(period=period, interval=interval)
    if hist.empty:
        return {"error": f"No price history for {ticker.ticker}.", "latest": None, "period": period, "interval": interval}

    close = hist["Close"]
    high = hist["High"]
    low = hist["Low"]

    # ── Simple Moving Averages ─────────────────────────────────────────────
    sma_20 = close.rolling(window=20).mean()
    sma_50 = close.rolling(window=50).mean()
    sma_200 = close.rolling(window=200).mean()

    # ── Exponential Moving Averages ────────────────────────────────────────
    ema_12 = close.ewm(span=12, adjust=False).mean()
    ema_26 = close.ewm(span=26, adjust=False).mean()

    # ── RSI (14-period) ───────────────────────────────────────────────────
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    # ── MACD ──────────────────────────────────────────────────────────────
    macd_line = ema_12 - ema_26
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_histogram = macd_line - macd_signal

    # ── Bollinger Bands (20-period, 2 std) ────────────────────────────────
    bb_middle = sma_20
    bb_std = close.rolling(window=20).std()
    bb_upper = bb_middle + (bb_std * 2)
    bb_lower = bb_middle - (bb_std * 2)

    # ── ATR (14-period) ──────────────────────────────────────────────────
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(window=14).mean()

    # Latest values (most recent complete data point)
    latest = {
        "date": hist.index[-1].strftime("%Y-%m-%d"),
        "close": _safe(close.iloc[-1]),
        "sma_20": _safe(sma_20.iloc[-1]),
        "sma_50": _safe(sma_50.iloc[-1]),
        "sma_200": _safe(sma_200.iloc[-1]),
        "ema_12": _safe(ema_12.iloc[-1]),
        "ema_26": _safe(ema_26.iloc[-1]),
        "rsi_14": _safe(rsi.iloc[-1]),
        "macd_line": _safe(macd_line.iloc[-1]),
        "macd_signal": _safe(macd_signal.iloc[-1]),
        "macd_histogram": _safe(macd_histogram.iloc[-1]),
        "bollinger_upper": _safe(bb_upper.iloc[-1]),
        "bollinger_middle": _safe(bb_middle.iloc[-1]),
        "bollinger_lower": _safe(bb_lower.iloc[-1]),
        "atr_14": _safe(atr.iloc[-1]),
    }

    # Signal interpretation
    signals = []
    price = close.iloc[-1]
    if pd.notna(sma_50.iloc[-1]) and pd.notna(sma_200.iloc[-1]):
        if sma_50.iloc[-1] > sma_200.iloc[-1]:
            signals.append("Golden Cross (SMA50 > SMA200) — bullish trend")
        else:
            signals.append("Death Cross (SMA50 < SMA200) — bearish trend")

    if pd.notna(rsi.iloc[-1]):
        rsi_val = rsi.iloc[-1]
        if rsi_val > 70:
            signals.append(f"RSI {rsi_val:.1f} — overbought territory")
        elif rsi_val < 30:
            signals.append(f"RSI {rsi_val:.1f} — oversold territory")
        else:
            signals.append(f"RSI {rsi_val:.1f} — neutral")

    if pd.notna(macd_histogram.iloc[-1]):
        if macd_histogram.iloc[-1] > 0:
            signals.append("MACD histogram positive — bullish momentum")
        else:
            signals.append("MACD histogram negative — bearish momentum")

    if pd.notna(bb_upper.iloc[-1]) and pd.notna(bb_lower.iloc[-1]):
        if price > bb_upper.iloc[-1]:
            signals.append("Price above upper Bollinger Band — potentially overbought")
        elif price < bb_lower.iloc[-1]:
            signals.append("Price below lower Bollinger Band — potentially oversold")

    latest["signals"] = signals

    return {
        "latest": latest,
        "period": period,
        "interval": interval,
    }


def get_holders(ticker: yf.Ticker) -> dict:
    """
    Institutional holders, mutual fund holders, insider transactions.
    Useful for gauging institutional conviction and insider sentiment.
    """
    inst = None
    try:
        inst = _df_to_records(ticker.institutional_holders)
    except Exception:
        pass

    mf = None
    try:
        mf = _df_to_records(ticker.mutualfund_holders)
    except Exception:
        pass

    major = None
    try:
        major = _df_to_records(ticker.major_holders)
    except Exception:
        pass

    insider_txn = None
    try:
        txn = ticker.insider_transactions
        if txn is not None and not txn.empty:
            insider_txn = _df_to_records(txn.head(20))
    except Exception:
        pass

    insider_purchases = None
    try:
        insider_purchases = _df_to_records(ticker.insider_purchases)
    except Exception:
        pass

    return {
        "major_holders": major,
        "institutional_holders": inst,
        "mutualfund_holders": mf,
        "insider_transactions_recent": insider_txn,
        "insider_purchases_summary": insider_purchases,
    }


# ── Orchestrator ───────────────────────────────────────────────────────────────
def pull_ticker_data(
    symbol: str,
    modules: list[str] | None = None,
    period: str = "1y",
    interval: str = "1d",
) -> dict:
    """
    Pull all requested data modules for a single ticker symbol.
    Returns a structured dict ready for JSON serialisation.
    """
    modules = modules or ALL_MODULES
    ticker = yf.Ticker(symbol)

    log.info("Pulling data for %s — modules: %s", symbol, ", ".join(modules))

    result = {"symbol": symbol.upper()}

    if "snapshot" in modules:
        log.info("  → snapshot")
        result["snapshot"] = get_snapshot(ticker)

    if "history" in modules:
        log.info("  → history (%s / %s)", period, interval)
        result["history"] = get_history(ticker, period=period, interval=interval)

    if "financials" in modules:
        log.info("  → financials")
        result["financials"] = get_financials(ticker)

    if "analysis" in modules:
        log.info("  → analysis")
        result["analysis"] = get_analysis(ticker)

    if "technicals" in modules:
        log.info("  → technicals")
        result["technicals"] = get_technicals(ticker, period=period, interval=interval)

    if "holders" in modules:
        log.info("  → holders")
        result["holders"] = get_holders(ticker)

    return result


# ── CLI ────────────────────────────────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description=(
            "AutoFund Analyst — Market Data Tool\n"
            "Pull structured financial data from Yahoo Finance for analysis."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "tickers",
        nargs="+",
        help="One or more ticker symbols (e.g. AAPL MSFT GOOGL)",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help=(
            "Output JSON file path. "
            "Default: analyst_data_<TICKER(S)>_<DATE>.json"
        ),
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print JSON to stdout instead of writing to a file.",
    )
    parser.add_argument(
        "--modules",
        nargs="+",
        choices=ALL_MODULES,
        default=None,
        help=(
            f"Which data modules to pull (default: all). "
            f"Options: {', '.join(ALL_MODULES)}"
        ),
    )
    parser.add_argument(
        "--period",
        default="1y",
        help=(
            "History period: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max "
            "(default: 1y)"
        ),
    )
    parser.add_argument(
        "--interval",
        default="1d",
        help=(
            "History interval: 1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, "
            "1wk, 1mo, 3mo (default: 1d)"
        ),
    )

    args = parser.parse_args()

    # Pull data for each ticker
    all_results = []
    for symbol in args.tickers:
        data = pull_ticker_data(
            symbol,
            modules=args.modules,
            period=args.period,
            interval=args.interval,
        )
        all_results.append(data)

    # Wrap in envelope
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tool": "AutoFund Analyst Market Data Tool",
        "parameters": {
            "tickers": [s.upper() for s in args.tickers],
            "modules": args.modules or ALL_MODULES,
            "period": args.period,
            "interval": args.interval,
        },
        "data": all_results if len(all_results) > 1 else all_results[0],
    }

    # Output
    json_str = json.dumps(output, indent=2, ensure_ascii=False)

    if args.stdout:
        print(json_str)
    else:
        if args.output:
            out_path = Path(args.output)
        else:
            tickers_slug = "_".join(s.upper() for s in args.tickers[:5])
            if len(args.tickers) > 5:
                tickers_slug += f"_+{len(args.tickers) - 5}"
            datestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
            out_path = Path(f"analyst_data_{tickers_slug}_{datestamp}.json")

        out_path.write_text(json_str, encoding="utf-8")
        log.info("✓ Saved data to %s", out_path.resolve())


if __name__ == "__main__":
    main()
