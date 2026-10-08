"""
AutoFund — Analyst Financial Models Library
===========================================
Pre-built financial calculation functions for the Analyst agent.

Each function supports two calling patterns:

  1. Explicit — pass all values directly (useful when you already have the data):
       dcf_valuation(fcf_per_share=8.42, growth_rate=0.12, ...)

  2. Ticker-based — pass just a ticker symbol and the function fetches
       only the modules it needs internally (saves tokens, no manual data extraction):
       dcf_valuation(ticker="AAPL")

The ticker-based pattern is the preferred Layer-1 workflow: one call, zero
token waste on intermediate data extraction.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from analyst.market_data import pull_ticker_data


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe(v: Any) -> Any:
    """Handle None, NaN, and non-serialisable types."""
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return None if (v != v or math.isnan(v)) else float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, float):
        return None if (v != v or math.isnan(v)) else v
    if isinstance(v, np.ndarray):
        return [_safe(x) for x in v.tolist()]
    if isinstance(v, list):
        return [_safe(x) for x in v]
    if isinstance(v, dict):
        return {k: _safe(vi) for k, vi in v.items()}
    return v


def _disc(rate: float, t: int) -> float:
    """Discount factor: 1 / (1 + rate)^t."""
    return 1.0 / (1.0 + rate) ** t


def _fetch_snapshot(ticker: str) -> dict:
    """Pull snapshot-only data for a ticker. Single module = minimal tokens."""
    data = pull_ticker_data(ticker, modules=["snapshot"])
    return data.get("snapshot", {})


def _fetch_tech_data(ticker: str, period: str = "1y") -> dict:
    """Pull price history for a ticker (used for technical analysis)."""
    return pull_ticker_data(ticker, modules=["history"], period=period)


# ── 1. DCF Valuation ─────────────────────────────────────────────────────────

def dcf_valuation(
    ticker: str | None = None,
    fcf_per_share: float | None = None,
    growth_rate: float | None = None,
    terminal_growth: float | None = None,
    wacc: float | None = None,
    shares_outstanding: float | None = None,
    years: int = 5,
) -> dict:
    """
    Two-stage DCF: explicit forecast period + Gordon Growth terminal value.

    Pattern A — ticker-based (preferred):
        dcf_valuation(ticker="AAPL")
        → pulls snapshot + financials internally; derives all parameters from data

    Pattern B — explicit:
        dcf_valuation(fcf_per_share=8.42, growth_rate=0.12, terminal_growth=0.025,
                      wacc=0.09, shares_outstanding=15.5e9, years=5)
        → all parameters passed manually

    Parameters (Pattern B — all optional if ticker provided)
    ----------
    ticker : str, optional
        Ticker symbol. When provided, all other parameters are derived from
        market data (snapshot + financials modules).
    fcf_per_share : float
        Trailing twelve-month free cash flow per share.
    growth_rate : float
        Annual FCF growth rate during the explicit forecast period.
    terminal_growth : float
        Long-run FCF growth rate for terminal value (typically 0.02–0.03).
    wacc : float
        Weighted average cost of capital / discount rate.
    shares_outstanding : float
        Shares outstanding.
    years : int, default 5
        Number of years in the explicit forecast stage.

    Returns
    -------
    dict
        - ``intrinsic_value_per_share``
        - ``upside_pct`` — vs. current market price
        - ``dcf_steps`` — stage-1 PV of FCFs, terminal value PV
        - ``sensitivity_table`` — intrinsic value across WACC x growth matrix
        - ``assumptions`` — all input parameters
        - ``data_source`` — ``"market_data"`` or ``"explicit"``
    """
    data_source = "market_data" if ticker else "explicit"
    current_price = None

    if ticker:
        snap = _fetch_snapshot(ticker)
        current_price = _safe(snap.get("current_price"))
        fin_data = pull_ticker_data(ticker, modules=["financials"])
        financials = fin_data.get("financials", {})
        is_annual = financials.get("income_statement_annual") or {}

        # Derive FCF per share
        raw_fcf = _safe(snap.get("free_cashflow") or snap.get("operating_cashflow"))
        shares = _safe(snap.get("shares_outstanding"))
        if raw_fcf and shares:
            fcf_per_share = raw_fcf / shares
        else:
            fcf_per_share = 0.0

        # Derive FCF growth from income statement (3-year CAGR on operating cash flow)
        cfo_series = _cashflow_series(financials)
        if len(cfo_series) >= 2:
            end_val = cfo_series[-1]
            begin_val = cfo_series[0]
            years_span = len(cfo_series) - 1
            if begin_val > 0 and years_span > 0:
                growth_rate = (end_val / begin_val) ** (1 / years_span) - 1
            else:
                growth_rate = 0.0
        else:
            growth_rate = 0.0

        # Derive WACC from beta via CAPM (10Y Treasury ~ 4.2%, market return ~ 10%)
        beta = _safe(snap.get("beta")) or 1.0
        risk_free = 0.042
        market_return = 0.10
        wacc = risk_free + (market_return - risk_free) * beta

        terminal_growth = 0.025  # conservative long-run assumption
        shares_outstanding = shares or 1.0

    # Validate — explicit mode requires these parameters
    if terminal_growth is None:
        raise ValueError("terminal_growth is required when no ticker is provided.")
    if wacc is None:
        raise ValueError("wacc is required when no ticker is provided.")
    if fcf_per_share is None:
        raise ValueError("fcf_per_share is required when no ticker is provided.")
    if growth_rate is None:
        raise ValueError("growth_rate is required when no ticker is provided.")
    if not (-1 < terminal_growth < wacc):
        raise ValueError(
            f"Terminal growth ({terminal_growth}) must be less than WACC ({wacc})."
        )
    if years < 1:
        raise ValueError("Forecast period (years) must be >= 1.")

    # Stage 1: project and discount FCFs
    stage1_cash_flows = []
    stage1_pv = []
    for t in range(1, years + 1):
        projected_fcf = fcf_per_share * (1 + growth_rate) ** t
        pv = projected_fcf * _disc(wacc, t)
        stage1_cash_flows.append(_safe(projected_fcf))
        stage1_pv.append(_safe(pv))

    stage1_total = sum(stage1_pv)

    # Terminal value
    fcf_terminal = stage1_cash_flows[-1] * (1 + terminal_growth)
    terminal_value = fcf_terminal / (wacc - terminal_growth)
    terminal_pv = terminal_value * _disc(wacc, years)
    intrinsic_value = stage1_total + terminal_pv

    # Upside vs. current market price (captured from the initial snapshot fetch)
    upside_pct = None
    if current_price:
        upside_pct = _safe(round((intrinsic_value - current_price) / current_price * 100, 2))

    # Sensitivity table
    sensitivity_rows = []
    wacc_range = [wacc - 0.02, wacc - 0.01, wacc, wacc + 0.01, wacc + 0.02]
    for w in wacc_range:
        if w <= terminal_growth:
            continue
        row = {"wacc": _safe(w)}
        cols = {}
        for g_label, g in [
            ("tg_minus_1pct", terminal_growth - 0.01),
            ("tg_actual", terminal_growth),
            ("tg_plus_1pct", terminal_growth + 0.01),
        ]:
            try:
                tg_pv = (stage1_cash_flows[-1] * (1 + g)) / (w - g)
                tv = tg_pv * _disc(w, years)
                cols[g_label] = _safe(round(stage1_total + tv, 2))
            except (ZeroDivisionError, ValueError):
                cols[g_label] = None
        row["values"] = cols
        sensitivity_rows.append(row)

    return {
        "function": "dcf_valuation",
        "ticker": ticker.upper() if ticker else None,
        "intrinsic_value_per_share": _safe(round(intrinsic_value, 2)),
        "current_price": current_price,
        "upside_pct": upside_pct,
        "dcf_steps": {
            "forecast_years": years,
            "wacc": _safe(wacc),
            "terminal_growth": _safe(terminal_growth),
            "stage1_cash_flows": stage1_cash_flows,
            "stage1_pv_of_cash_flows": stage1_pv,
            "stage1_total_pv": _safe(round(stage1_total, 4)),
            "terminal_value": _safe(round(terminal_value, 2)),
            "terminal_value_pv": _safe(round(terminal_pv, 2)),
            "equity_value_per_share": _safe(round(intrinsic_value, 2)),
        },
        "sensitivity_table": sensitivity_rows,
        "assumptions": {
            "fcf_per_share": _safe(round(fcf_per_share, 4)),
            "growth_rate": _safe(growth_rate),
            "terminal_growth": _safe(terminal_growth),
            "wacc": _safe(wacc),
            "shares_outstanding": _safe(shares_outstanding),
            "forecast_years": years,
        },
        "data_source": data_source,
    }


def _cashflow_series(financials: dict) -> list[float]:
    """
    Extract annual free cash flow series (oldest-first) from financials dict.

    yfinance row labels: 'Free Cash Flow', 'Operating Cash Flow'
    """
    cfo = financials.get("cash_flow_annual") or {}
    if not cfo:
        return []
    dates = sorted(cfo.keys())  # oldest first for correct CAGR calculation
    values = []
    for date_key in dates:
        row = cfo.get(date_key, {})
        # Try 'Free Cash Flow' first (preferred), then 'Operating Cash Flow'
        val = row.get("Free Cash Flow") or row.get("Operating Cash Flow")
        if val is not None and val > 0:
            values.append(float(val))
    return values


def _revenue_series(is_annual: dict) -> list[float]:
    """
    Extract annual total revenue series (oldest-first) from income statement dict.

    yfinance row label: 'Total Revenue'
    """
    if not is_annual:
        return []
    dates = sorted(is_annual.keys())  # oldest first for correct CAGR calculation
    values = []
    for d in dates:
        row = is_annual.get(d, {})
        val = row.get("Total Revenue") or row.get("Operating Revenue")
        if val is not None and val > 0:
            values.append(float(val))
    return values


def _latest_annual(is_annual: dict, field: str) -> float | None:
    """
    Extract the latest annual value for a row label from income statement dict.

    yfinance field names: 'Total Revenue', 'Operating Income', 'Net Income',
    'Gross Profit', 'EBITDA', 'EBIT', etc.
    """
    if not is_annual:
        return None
    dates = sorted(is_annual.keys(), reverse=True)  # newest first
    for d in dates:
        row = is_annual.get(d, {})
        val = row.get(field)
        if val is not None and val > 0:
            return float(val)
    return None


# ── 2. Projected Income Statement ────────────────────────────────────────────

def projected_income_statement(
    ticker: str | None = None,
    revenue: float | None = None,
    growth_rate: float | None = None,
    opex_ratio: float | None = None,
    years: int = 5,
    tax_rate: float = 0.21,
    depreciation_pct: float = 0.03,
    interest_pct: float = 0.01,
) -> dict:
    """
    Year-by-year P&L projection.

    Pattern A — ticker-based (preferred):
        projected_income_statement(ticker="AAPL")
        → derives revenue, growth_rate, opex_ratio from market data

    Pattern B — explicit:
        projected_income_statement(revenue=394e9, growth_rate=0.08, opex_ratio=0.68)
    """
    data_source = "market_data" if ticker else "explicit"

    if ticker:
        snap = _fetch_snapshot(ticker)
        fin_data = pull_ticker_data(ticker, modules=["financials"])
        financials = fin_data.get("financials", {})
        is_annual = financials.get("income_statement_annual") or {}

        # Revenue: most recent annual totalRevenue
        revenue = _latest_annual(is_annual, "Total Revenue") or 0.0

        # Operating margin → opex_ratio = 1 - operating_margin
        op_margin = _safe(snap.get("operating_margins"))
        if op_margin:
            opex_ratio = 1.0 - op_margin
        else:
            opex_ratio = 0.68  # fallback

        # Revenue growth: 3-year CAGR on totalRevenue
        rev_series = _revenue_series(is_annual)
        if len(rev_series) >= 2:
            n = len(rev_series) - 1
            growth_rate = (rev_series[-1] / rev_series[0]) ** (1 / n) - 1 if rev_series[0] > 0 else 0.0
        else:
            growth_rate = 0.0

    if revenue is None:
        raise ValueError("revenue is required when no ticker is provided")
    if growth_rate is None:
        raise ValueError("growth_rate is required when no ticker is provided")
    if opex_ratio is None:
        raise ValueError("opex_ratio is required when no ticker is provided")
    # Clamp opex_ratio to a safe range — real market data can produce values
    # outside (0, 1) for loss-making or hyper-profitable companies
    opex_ratio = max(0.05, min(0.95, opex_ratio))
    if depreciation_pct >= opex_ratio:
        depreciation_pct = opex_ratio * 0.1  # safe fallback

    rows = []
    year_labels = [f"Y{t}" for t in range(1, years + 1)]

    prev_rev = revenue
    for t in range(1, years + 1):
        rev = revenue * (1 + growth_rate) ** t
        ebitda = rev * (1 - opex_ratio + depreciation_pct)
        ebit = ebitda - rev * depreciation_pct
        ebt = ebit - rev * interest_pct
        net_income = ebt * (1 - tax_rate)
        fcf_approx = net_income + rev * depreciation_pct

        rev_growth_pct = _safe(round(((rev / prev_rev) - 1) * 100, 2)) if prev_rev else None
        prev_rev = rev
        rows.append({
            "year": year_labels[t - 1],
            "revenue": _safe(round(rev, 0)),
            "revenue_growth_pct": rev_growth_pct,
            "ebitda_margin_pct": _safe(round((ebitda / rev) * 100, 2)),
            "ebit_margin_pct": _safe(round((ebit / rev) * 100, 2)),
            "net_income_margin_pct": _safe(round((net_income / rev) * 100, 2)),
            "fcf_approx": _safe(round(fcf_approx, 0)),
        })

    return {
        "function": "projected_income_statement",
        "ticker": ticker.upper() if ticker else None,
        "projection": rows,
        "assumptions": {
            "base_revenue": _safe(revenue),
            "growth_rate": _safe(growth_rate),
            "opex_ratio": _safe(opex_ratio),
            "tax_rate": _safe(tax_rate),
            "depreciation_pct": _safe(depreciation_pct),
            "interest_pct": _safe(interest_pct),
            "years": years,
        },
        "data_source": data_source,
    }


# ── 3. Technical Summary ─────────────────────────────────────────────────────

def technical_summary(
    ticker: str | None = None,
    prices_df: pd.DataFrame | list[dict] | None = None,
    period: str = "1y",
) -> dict:
    """
    Compute technical indicators from OHLCV data.

    Pattern A — ticker-based (preferred):
        technical_summary(ticker="AAPL")
        → pulls 1-year daily history internally

    Pattern B — explicit:
        technical_summary(prices_df=df)   # DataFrame or list of dicts with Close
    """
    data_source = "market_data" if ticker else "explicit"

    if ticker:
        hist_data = _fetch_tech_data(ticker, period=period)
        raw = hist_data.get("history", {}).get("data")
        if not raw:
            return {"error": f"No price history returned for '{ticker}'."}
        prices_df = raw

    if prices_df is None:
        raise ValueError("prices_df is required when no ticker is provided.")

    if isinstance(prices_df, list):
        prices_df = pd.DataFrame(prices_df)

    required = ["Close"]
    missing = [c for c in required if c not in prices_df.columns]
    if missing:
        raise ValueError(f"prices_df must contain columns: {required}. Missing: {missing}")

    close = prices_df["Close"].dropna()
    high = prices_df.get("High", close)
    low = prices_df.get("Low", close)
    volume = prices_df.get("Volume")

    n = len(close)
    if n < 2:
        return {"error": "Insufficient data points — need at least 2."}

    # Moving averages
    sma_20 = close.rolling(window=min(20, n)).mean() if n >= 20 else close
    sma_50 = close.rolling(window=min(50, n)).mean() if n >= 50 else close
    sma_200 = close.rolling(window=min(200, n)).mean() if n >= 200 else close

    # RSI 14
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=min(14, n)).mean()
    avg_loss = loss.rolling(window=min(14, n)).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    # MACD 12/26/9
    ema_12 = close.ewm(span=min(12, n), adjust=False).mean() if n >= 12 else close
    ema_26 = close.ewm(span=min(26, n), adjust=False).mean() if n >= 26 else close
    macd_line = ema_12 - ema_26
    macd_signal = macd_line.ewm(span=min(9, n), adjust=False).mean() if n >= 9 else macd_line
    macd_hist = macd_line - macd_signal

    # Bollinger Bands 20/2
    bb_middle = sma_20 if n >= 20 else close
    bb_std = close.rolling(window=min(20, n)).std()
    bb_upper = bb_middle + bb_std * 2
    bb_lower = bb_middle - bb_std * 2

    # ATR 14
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(window=min(14, n)).mean()

    # Latest values
    price = float(close.iloc[-1])
    latest = {
        "date": str(close.index[-1]) if hasattr(close.index[-1], "strftime") else str(close.iloc[-1]),
        "close": _safe(price),
        "sma_20": _safe(float(sma_20.iloc[-1])) if n >= 20 else None,
        "sma_50": _safe(float(sma_50.iloc[-1])) if n >= 50 else None,
        "sma_200": _safe(float(sma_200.iloc[-1])) if n >= 200 else None,
        "ema_12": _safe(float(ema_12.iloc[-1])) if n >= 12 else None,
        "ema_26": _safe(float(ema_26.iloc[-1])) if n >= 26 else None,
        "rsi_14": _safe(float(rsi.iloc[-1])) if n >= 14 else None,
        "macd_line": _safe(float(macd_line.iloc[-1])) if n >= 26 else None,
        "macd_signal": _safe(float(macd_signal.iloc[-1])) if n >= 26 else None,
        "macd_histogram": _safe(float(macd_hist.iloc[-1])) if n >= 26 else None,
        "bollinger_upper": _safe(float(bb_upper.iloc[-1])) if n >= 20 else None,
        "bollinger_middle": _safe(float(bb_middle.iloc[-1])) if n >= 20 else None,
        "bollinger_lower": _safe(float(bb_lower.iloc[-1])) if n >= 20 else None,
        "atr_14": _safe(float(atr.iloc[-1])) if n >= 14 else None,
        "volume_avg_20d": (
            _safe(float(volume.rolling(min(20, n)).mean().iloc[-1]))
            if volume is not None and n >= 20 else None
        ),
    }

    # Signals
    signals = []
    if n >= 50 and pd.notna(sma_50.iloc[-1]) and pd.notna(sma_200.iloc[-1]):
        if sma_50.iloc[-1] > sma_200.iloc[-1]:
            signals.append("Golden Cross (SMA50 > SMA200) — bullish trend signal")
        else:
            signals.append("Death Cross (SMA50 < SMA200) — bearish trend signal")

    if n >= 20 and pd.notna(bb_upper.iloc[-1]) and pd.notna(bb_lower.iloc[-1]):
        if price > float(bb_upper.iloc[-1]):
            signals.append("Price above upper Bollinger Band — potentially overbought")
        elif price < float(bb_lower.iloc[-1]):
            signals.append("Price below lower Bollinger Band — potentially oversold")

    rsi_val = rsi.iloc[-1] if n >= 14 else None
    if rsi_val is not None and pd.notna(rsi_val):
        if rsi_val > 70:
            signals.append(f"RSI {rsi_val:.1f} — overbought territory")
        elif rsi_val < 30:
            signals.append(f"RSI {rsi_val:.1f} — oversold territory")
        else:
            signals.append(f"RSI {rsi_val:.1f} — neutral zone")

    macd_hist_val = macd_hist.iloc[-1] if n >= 26 else None
    if macd_hist_val is not None and pd.notna(macd_hist_val):
        signals.append(
            "MACD histogram positive — bullish momentum"
            if macd_hist_val > 0
            else "MACD histogram negative — bearish momentum"
        )

    if n >= 20 and pd.notna(sma_20.iloc[-1]):
        signals.append(
            "Price above SMA20 — short-term bullish"
            if close.iloc[-1] > float(sma_20.iloc[-1])
            else "Price below SMA20 — short-term bearish"
        )

    latest["signals"] = signals

    # Trend interpretation
    trend = "unknown"
    if n >= 200 and pd.notna(sma_200.iloc[-1]) and pd.notna(sma_50.iloc[-1]):
        if price > float(sma_200.iloc[-1]) and float(sma_50.iloc[-1]) > float(sma_200.iloc[-1]):
            trend = "strong uptrend"
        elif price < float(sma_200.iloc[-1]) and float(sma_50.iloc[-1]) < float(sma_200.iloc[-1]):
            trend = "strong downtrend"
        elif price > float(sma_200.iloc[-1]):
            trend = "mid-term bullish"
        else:
            trend = "mid-term bearish"
    elif n >= 50 and pd.notna(sma_50.iloc[-1]):
        trend = "short-term bullish" if price > float(sma_50.iloc[-1]) else "short-term bearish"

    latest["trend_interpretation"] = trend

    return {
        "function": "technical_summary",
        "ticker": ticker.upper() if ticker else None,
        "latest": latest,
        "assumptions": {
            "period": period,
            "lookback_sma_20": 20,
            "lookback_sma_50": 50,
            "lookback_sma_200": 200,
            "lookback_rsi": 14,
            "lookback_macd_fast": 12,
            "lookback_macd_slow": 26,
            "lookback_macd_signal": 9,
            "lookback_bollinger": 20,
            "lookback_atr": 14,
        },
        "data_source": data_source,
    }


# ── 4. Peer Valuation Multiples ───────────────────────────────────────────────

def peer_valuation_multiples(
    ticker: str | None = None,
    peer_tickers: list[str] | None = None,
    snapshot: dict | None = None,
    peer_snapshots: list[dict] | None = None,
) -> dict:
    """
    Compare key valuation multiples across a ticker and its peers.

    Pattern A — ticker-based (preferred):
        peer_valuation_multiples(ticker="AAPL", peer_tickers=["MSFT", "GOOGL"])
        → fetches snapshot for all 3 tickers internally, one call each

    Pattern B — explicit:
        peer_valuation_multiples(snapshot=snap_aapl, peer_snapshots=[snap_msft, snap_googl])
        → pass pre-fetched snapshot dicts
    """
    data_source = "market_data" if ticker else "explicit"

    if ticker:
        primary = _fetch_snapshot(ticker)
        primary["symbol"] = ticker.upper()
        peers = []
        for pt in peer_tickers or []:
            p = _fetch_snapshot(pt)
            p["symbol"] = pt.upper()
            peers.append(p)
    else:
        primary = snapshot or {}
        peers = peer_snapshots or []

    def row(snap: dict) -> dict:
        def sv(*keys, default=None):
            for k in keys:
                v = snap.get(k)
                if v is not None:
                    return v
            return default

        return {
            "symbol": snap.get("symbol"),
            "current_price": _safe(sv("current_price")),
            "market_cap": _safe(sv("market_cap")),
            "trailing_pe": _safe(sv("trailing_pe")),
            "forward_pe": _safe(sv("forward_pe")),
            "peg_ratio": _safe(sv("peg_ratio")),
            "price_to_book": _safe(sv("price_to_book")),
            "ev_to_ebitda": _safe(sv("ev_to_ebitda")),
            "ev_to_revenue": _safe(sv("ev_to_revenue")),
            "gross_margin_pct": _safe(sv("gross_margins")),
            "operating_margin_pct": _safe(sv("operating_margins")),
            "profit_margin_pct": _safe(sv("profit_margins")),
            "roe_pct": _safe(sv("return_on_equity")),
            "debt_to_equity": _safe(sv("debt_to_equity")),
            "beta": _safe(sv("beta")),
            "analyst_target": _safe(sv("target_mean_price")),
        }

    ticker_row = row(primary)
    peer_rows = [row(p) for p in peers]
    all_rows = [ticker_row] + peer_rows

    metrics = [
        ("trailing_pe", "Trailing P/E"),
        ("forward_pe", "Forward P/E"),
        ("peg_ratio", "PEG Ratio"),
        ("price_to_book", "Price/Book"),
        ("ev_to_ebitda", "EV/EBITDA"),
        ("ev_to_revenue", "EV/Revenue"),
        ("gross_margin_pct", "Gross Margin %"),
        ("operating_margin_pct", "Operating Margin %"),
        ("profit_margin_pct", "Net Profit Margin %"),
        ("roe_pct", "ROE %"),
        ("debt_to_equity", "Debt/Equity"),
        ("beta", "Beta"),
    ]

    comparisons = {}
    for key, label in metrics:
        vals = [r[key] for r in all_rows if r.get(key) is not None]
        ticker_val = ticker_row.get(key)
        avg = np.mean(vals) if vals else None
        med = np.median(vals) if vals else None

        verdict = "neutral"
        if ticker_val is not None and med is not None and med != 0:
            if ticker_val < med * 0.8:
                verdict = "cheap vs. peers"
            elif ticker_val > med * 1.2:
                verdict = "premium vs. peers"
        elif ticker_val is not None and avg is not None and avg != 0:
            if ticker_val < avg * 0.8:
                verdict = "cheap vs. peers"
            elif ticker_val > avg * 1.2:
                verdict = "premium vs. peers"

        comparisons[key] = {
            "metric": label,
            "ticker_value": _safe(ticker_val),
            "peer_avg": _safe(round(avg, 2) if avg else None),
            "peer_median": _safe(round(med, 2) if med else None),
            "verdict": verdict,
        }

    return {
        "function": "peer_valuation_multiples",
        "ticker": ticker.upper() if ticker else ticker_row.get("symbol"),
        "ticker_row": ticker_row,
        "peer_rows": peer_rows,
        "comparisons": comparisons,
        "assumptions": {
            "peer_count": len(peers),
            "verdict_thresholds": {
                "cheap": "< 80% of peer median",
                "premium": "> 120% of peer median",
                "neutral": "between",
            },
        },
        "data_source": data_source,
    }


# ── 5. Scenario Model ─────────────────────────────────────────────────────────

def scenario_model(
    ticker: str | None = None,
    base_assumptions: dict | None = None,
    bull_assumptions: dict | None = None,
    bear_assumptions: dict | None = None,
) -> dict:
    """
    Three-scenario price target model (bull / base / bear).

    Pattern A — ticker-based (preferred):
        scenario_model(ticker="AAPL")
        → derives base_assumptions from market data; bull/bear are derived
          from base with +/- adjustments to growth and margins

    Pattern B — explicit:
        scenario_model(base_assumptions={...}, bull_assumptions={...})
    """
    data_source = "market_data" if ticker else "explicit"

    if ticker:
        snap = _fetch_snapshot(ticker)
        fin_data = pull_ticker_data(ticker, modules=["financials"])
        financials = fin_data.get("financials", {})
        is_annual = financials.get("income_statement_annual") or {}

        shares = _safe(snap.get("shares_outstanding")) or 1.0
        current_price = _safe(snap.get("current_price")) or 0.0
        revenue = _latest_annual(is_annual, "Total Revenue") or 0.0

        op_margin = _safe(snap.get("operating_margins")) or 0.25
        opex_base = 1.0 - op_margin

        rev_series = _revenue_series(is_annual)
        if len(rev_series) >= 2:
            n = len(rev_series) - 1
            growth_base = (rev_series[-1] / rev_series[0]) ** (1 / n) - 1 if rev_series[0] > 0 else 0.08
        else:
            growth_base = 0.08

        # Base: analyst consensus forward P/E as multiple, else trailing P/E
        fwd_pe = _safe(snap.get("forward_pe"))
        trail_pe = _safe(snap.get("trailing_pe"))
        multiple = fwd_pe or trail_pe or 25.0

        base_assumptions = {
            "revenue": revenue,
            "growth_rate": growth_base,
            "opex_ratio": opex_base,
            "shares_outstanding": shares,
            "current_price": current_price,
            "multiple": multiple,
            "years": 5,
        }
        # Bull: +50% growth, +3pp margin, +20% multiple
        bull_assumptions = {
            **base_assumptions,
            "growth_rate": growth_base * 1.5,
            "opex_ratio": max(0.05, opex_base - 0.05),
            "multiple": multiple * 1.2,
        }
        # Bear: -50% growth, +5pp opex (worse margins), -20% multiple
        bear_assumptions = {
            **base_assumptions,
            "growth_rate": max(0.0, growth_base * 0.5),
            "opex_ratio": min(0.95, opex_base + 0.05),
            "multiple": multiple * 0.8,
        }

    if base_assumptions is None:
        raise ValueError("base_assumptions is required when no ticker is provided")

    def project(scenario_name: str, assumptions: dict, conviction: str) -> dict:
        rev = assumptions["revenue"]
        gr = assumptions["growth_rate"]
        opex = assumptions["opex_ratio"]
        shares = assumptions["shares_outstanding"]
        cur_price = assumptions["current_price"]
        multiple = assumptions["multiple"]
        years = assumptions.get("years", 5)
        tax_rate = assumptions.get("tax_rate", 0.21)
        dep_pct = assumptions.get("depreciation_pct", 0.03)
        int_pct = assumptions.get("interest_pct", 0.01)

        rev_5 = rev * (1 + gr) ** years
        ebitda_5 = rev_5 * (1 - opex + dep_pct)
        ebit_5 = ebitda_5 - rev_5 * dep_pct
        ebt_5 = ebit_5 - rev_5 * int_pct
        net_income_5 = ebt_5 * (1 - tax_rate)
        terminal_value = net_income_5 * multiple
        implied_price = terminal_value / shares
        upside = (implied_price - cur_price) / cur_price * 100 if cur_price else None

        return {
            "scenario": scenario_name,
            "conviction": conviction,
            "year_5_revenue": _safe(round(rev_5, 0)),
            "year_5_ebitda_margin_pct": _safe(round((ebitda_5 / rev_5) * 100, 2)),
            "year_5_net_income": _safe(round(net_income_5, 0)),
            "target_multiple": multiple,
            "terminal_value": _safe(round(terminal_value, 0)),
            "implied_price_per_share": _safe(round(implied_price, 2)),
            "upside_pct": _safe(round(upside, 2)) if upside is not None else None,
            "assumptions": {k: _safe(v) for k, v in assumptions.items()},
        }

    scenarios = []
    scenarios.append(project("base", base_assumptions, "high"))
    if bull_assumptions:
        scenarios.append(project("bullish", bull_assumptions, "medium"))
    if bear_assumptions:
        scenarios.append(project("bearish", bear_assumptions, "medium"))

    return {
        "function": "scenario_model",
        "ticker": ticker.upper() if ticker else None,
        "scenarios": scenarios,
        "data_source": data_source,
    }


# ── 6. CAPM ──────────────────────────────────────────────────────────────────

def capm(
    risk_free_rate: float,
    market_return: float,
    beta: float,
) -> dict:
    """
    Capital Asset Pricing Model — required return on equity.
    All parameters must be passed explicitly (no market data needed).
    """
    if beta < 0:
        raise ValueError(f"Beta ({beta}) must be non-negative.")

    erp = market_return - risk_free_rate
    risk_adjusted_premium = erp * beta
    required_return = risk_free_rate + risk_adjusted_premium

    return {
        "function": "capm",
        "required_return": _safe(round(required_return, 4)),
        "required_return_pct": _safe(round(required_return * 100, 2)),
        "equity_risk_premium": _safe(round(erp, 4)),
        "equity_risk_premium_pct": _safe(round(erp * 100, 2)),
        "beta_adjusted_premium": _safe(round(risk_adjusted_premium, 4)),
        "beta_adjusted_premium_pct": _safe(round(risk_adjusted_premium * 100, 2)),
        "assumptions": {
            "risk_free_rate": _safe(risk_free_rate),
            "market_return": _safe(market_return),
            "beta": _safe(beta),
        },
        "data_source": "explicit",
    }


# ── 7. Dividend Discount Model ────────────────────────────────────────────────

def dividend_discount_model(
    ticker: str | None = None,
    current_price: float | None = None,
    current_dividend: float | None = None,
    dividend_growth_rate: float | None = None,
    required_return: float | None = None,
) -> dict:
    """
    Gordon Growth Model — intrinsic value from dividends.

    Pattern A — ticker-based (preferred):
        dividend_discount_model(ticker="JNJ", required_return=0.09)
        → fetches current_price and dividend_yield from market data;
          growth_rate and required_return derived if not provided

    Pattern B — explicit:
        dividend_discount_model(current_price=165.0, current_dividend=4.76,
                                 dividend_growth_rate=0.06, required_return=0.09)
    """
    data_source = "market_data" if ticker else "explicit"

    if ticker:
        snap = _fetch_snapshot(ticker)
        current_price = _safe(snap.get("current_price"))
        div_rate = _safe(snap.get("dividend_rate"))
        div_yield = _safe(snap.get("dividend_yield"))
        fwd_eps = _safe(snap.get("forward_eps"))
        trail_eps = _safe(snap.get("trailing_eps"))
        beta = _safe(snap.get("beta")) or 1.0

        # Current dividend: dividend_rate (annual) or yield × price
        if div_rate:
            current_dividend = div_rate
        elif div_yield and current_price:
            current_dividend = div_yield * current_price
        else:
            current_dividend = 0.0

        # Required return: CAPM if not given (must be computed before growth guard)
        if required_return is None:
            risk_free = 0.042
            market_return = 0.10
            required_return = risk_free + (market_return - risk_free) * beta

        # Dividend growth: 3-year expected from growth estimates if available,
        # else derive from retention ratio × ROE approximation
        growth = _safe(snap.get("earnings_growth"))
        if not growth:
            if fwd_eps and trail_eps and trail_eps > 0:
                growth = (fwd_eps / trail_eps) - 1
            else:
                growth = 0.06  # fallback
        dividend_growth_rate = growth

        # Guard: if estimated growth ≥ required return, DDM is invalid.
        # Cap to 1bp below required_return and flag it.
        if dividend_growth_rate >= required_return:
            dividend_growth_rate = required_return - 0.001

    if current_price is None:
        raise ValueError("current_price is required")
    if current_dividend is None:
        raise ValueError("current_dividend is required")
    if required_return is None:
        raise ValueError("required_return is required")
    if dividend_growth_rate is None:
        raise ValueError("dividend_growth_rate is required")
    if dividend_growth_rate >= required_return:
        raise ValueError(
            f"Dividend growth ({dividend_growth_rate}) must be less than "
            f"required return ({required_return})."
        )
    if current_dividend <= 0:
        # Non-dividend-paying stocks: return a structured result instead of crashing
        return {
            "function": "dividend_discount_model",
            "ticker": ticker.upper() if ticker else None,
            "error": "DDM is not applicable — this stock does not pay a dividend.",
            "current_price": _safe(current_price),
            "current_dividend": _safe(current_dividend),
            "data_source": data_source,
        }

    div1 = current_dividend * (1 + dividend_growth_rate)
    intrinsic = div1 / (required_return - dividend_growth_rate)
    upside = (intrinsic - current_price) / current_price * 100

    verdict = "fair value"
    if upside > 10:
        verdict = "undervalued"
    elif upside < -10:
        verdict = "overvalued"

    return {
        "function": "dividend_discount_model",
        "ticker": ticker.upper() if ticker else None,
        "intrinsic_value_per_share": _safe(round(intrinsic, 2)),
        "current_price": _safe(current_price),
        "current_dividend": _safe(current_dividend),
        "next_year_dividend": _safe(round(div1, 4)),
        "upside_pct": _safe(round(upside, 2)),
        "verdict": verdict,
        "assumptions": {
            "current_price": _safe(current_price),
            "current_dividend": _safe(current_dividend),
            "dividend_growth_rate": _safe(dividend_growth_rate),
            "required_return": _safe(required_return),
        },
        "data_source": data_source,
    }


# ── 8. Compound Growth Rate ───────────────────────────────────────────────────

def compound_growth_rate(
    beginning_value: float,
    ending_value: float,
    years: float,
) -> dict:
    """
    CAGR — compound annual growth rate between two values.
    All parameters must be passed explicitly (no market data fetching needed).
    """
    if beginning_value <= 0:
        raise ValueError("beginning_value must be positive.")
    if years <= 0:
        raise ValueError("years must be positive.")

    cagr = (ending_value / beginning_value) ** (1 / years) - 1
    total_growth = (ending_value - beginning_value) / beginning_value

    return {
        "function": "compound_growth_rate",
        "cagr": _safe(round(cagr, 6)),
        "cagr_pct": _safe(round(cagr * 100, 2)),
        "total_growth_pct": _safe(round(total_growth * 100, 2)),
        "assumptions": {
            "beginning_value": _safe(beginning_value),
            "ending_value": _safe(ending_value),
            "years": _safe(years),
        },
        "data_source": "explicit",
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, json

    parser = argparse.ArgumentParser(
        description="AutoFund Analyst Financial Models Library — CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="model", required=True)

    p_dcf = sub.add_parser("dcf", help="Two-stage DCF valuation")
    p_dcf.add_argument("--ticker", type=str, default=None, help="Ticker symbol (fetches data automatically)")
    p_dcf.add_argument("--fcf", type=float, default=None, help="FCF per share (TTM)")
    p_dcf.add_argument("--growth", type=float, default=None, help="Annual FCF growth rate")
    p_dcf.add_argument("--terminal-growth", type=float, default=None, help="Terminal growth rate (e.g. 0.025, auto-derived when --ticker used)")
    p_dcf.add_argument("--wacc", type=float, default=None, help="WACC / discount rate")
    p_dcf.add_argument("--shares", type=float, default=None, help="Shares outstanding")
    p_dcf.add_argument("--years", type=int, default=5, help="Forecast period (default 5)")

    p_is = sub.add_parser("income-statement", help="Projected income statement")
    p_is.add_argument("--ticker", type=str, default=None, help="Ticker symbol (fetches data automatically)")
    p_is.add_argument("--revenue", type=float, default=None, help="Current annual revenue")
    p_is.add_argument("--growth", type=float, default=None, help="Revenue growth rate")
    p_is.add_argument("--opex", type=float, default=None, help="OpEx / revenue ratio (e.g. 0.68)")
    p_is.add_argument("--years", type=int, default=5, help="Projection years (default 5)")

    p_cagr = sub.add_parser("cagr", help="Compound annual growth rate")
    p_cagr.add_argument("--begin", type=float, required=True, help="Beginning value")
    p_cagr.add_argument("--end", type=float, required=True, help="Ending value")
    p_cagr.add_argument("--years", type=float, required=True, help="Number of years")

    p_capm = sub.add_parser("capm", help="CAPM required return on equity")
    p_capm.add_argument("--rf", type=float, required=True, help="Risk-free rate (e.g. 0.042)")
    p_capm.add_argument("--rm", type=float, required=True, help="Expected market return (e.g. 0.10)")
    p_capm.add_argument("--beta", type=float, required=True, help="Asset beta")

    p_ddm = sub.add_parser("ddm", help="Dividend discount model (Gordon Growth)")
    p_ddm.add_argument("--ticker", type=str, default=None, help="Ticker symbol (fetches data automatically)")
    p_ddm.add_argument("--price", type=float, default=None, help="Current share price")
    p_ddm.add_argument("--div", type=float, default=None, help="Current TTM dividend per share")
    p_ddm.add_argument("--g", type=float, default=None, help="Dividend growth rate")
    p_ddm.add_argument("--r", type=float, default=None, help="Required return (cost of equity)")

    p_tech = sub.add_parser("technical", help="Technical summary")
    p_tech.add_argument("--ticker", type=str, required=True, help="Ticker symbol")
    p_tech.add_argument("--period", type=str, default="1y", help="History period (default 1y)")

    p_peer = sub.add_parser("peer", help="Peer valuation multiples")
    p_peer.add_argument("--ticker", type=str, required=True, help="Primary ticker")
    p_peer.add_argument("--peers", type=str, required=True, help="Comma-separated peer tickers (e.g. MSFT,GOOGL)")

    p_scenario = sub.add_parser("scenario", help="Scenario model")
    p_scenario.add_argument("--ticker", type=str, required=True, help="Ticker symbol")

    args = parser.parse_args()

    if args.model == "dcf":
        result = dcf_valuation(
            ticker=args.ticker,
            fcf_per_share=args.fcf,
            growth_rate=args.growth,
            terminal_growth=args.terminal_growth,
            wacc=args.wacc,
            shares_outstanding=args.shares,
            years=args.years,
        )

    elif args.model == "income-statement":
        result = projected_income_statement(
            ticker=args.ticker,
            revenue=args.revenue,
            growth_rate=args.growth,
            opex_ratio=args.opex,
            years=args.years,
        )

    elif args.model == "cagr":
        result = compound_growth_rate(args.begin, args.end, args.years)

    elif args.model == "capm":
        result = capm(args.rf, args.rm, args.beta)

    elif args.model == "ddm":
        result = dividend_discount_model(
            ticker=args.ticker,
            current_price=args.price,
            current_dividend=args.div,
            dividend_growth_rate=args.g,
            required_return=args.r,
        )

    elif args.model == "technical":
        result = technical_summary(ticker=args.ticker, period=args.period)

    elif args.model == "peer":
        peers = [p.strip() for p in args.peers.split(",")]
        result = peer_valuation_multiples(ticker=args.ticker, peer_tickers=peers)

    elif args.model == "scenario":
        result = scenario_model(ticker=args.ticker)

    print(json.dumps(result, indent=2, ensure_ascii=False))