"""
Tests for the Analyst package — models, market_data helpers, and interpreter.
These tests use explicit parameters (no network calls to yfinance).
"""

import math
import json
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest


# ── _safe helper ─────────────────────────────────────────────────────────────


class TestSafeHelper:
    """Test the _safe JSON serialisation helper in market_data.py."""

    def test_none(self):
        from analyst.market_data import _safe
        assert _safe(None) is None

    def test_nan_float(self):
        from analyst.market_data import _safe
        assert _safe(float("nan")) is None

    def test_normal_float(self):
        from analyst.market_data import _safe
        assert _safe(3.14) == 3.14

    def test_np_integer(self):
        from analyst.market_data import _safe
        assert _safe(np.int64(42)) == 42
        assert isinstance(_safe(np.int64(42)), int)

    def test_np_float(self):
        from analyst.market_data import _safe
        assert _safe(np.float64(2.5)) == 2.5
        assert isinstance(_safe(np.float64(2.5)), float)

    def test_np_nan(self):
        from analyst.market_data import _safe
        assert _safe(np.float64("nan")) is None

    def test_np_bool(self):
        from analyst.market_data import _safe
        assert _safe(np.bool_(True)) is True
        assert isinstance(_safe(np.bool_(True)), bool)

    def test_datetime(self):
        from analyst.market_data import _safe
        from datetime import datetime
        dt = datetime(2026, 4, 22, 10, 30)
        assert _safe(dt) == dt.isoformat()

    def test_list(self):
        from analyst.market_data import _safe
        assert _safe([1, float("nan"), 3]) == [1, None, 3]

    def test_ndarray(self):
        from analyst.market_data import _safe
        arr = np.array([1.0, 2.0, float("nan")])
        result = _safe(arr)
        assert result == [1.0, 2.0, None]


# ── _df_to_records ───────────────────────────────────────────────────────────


class TestDfToRecords:

    def test_empty_df(self):
        from analyst.market_data import _df_to_records
        assert _df_to_records(pd.DataFrame()) is None
        assert _df_to_records(None) is None

    def test_normal_df(self):
        from analyst.market_data import _df_to_records
        df = pd.DataFrame({"A": [1, 2], "B": [3.0, float("nan")]})
        result = _df_to_records(df)
        assert len(result) == 2
        assert result[0]["A"] == 1
        assert result[1]["B"] is None  # NaN → None


# ── CAPM ─────────────────────────────────────────────────────────────────────


class TestCAPM:

    def test_basic_capm(self):
        from analyst.models import capm
        result = capm(risk_free_rate=0.04, market_return=0.10, beta=1.0)
        assert result["function"] == "capm"
        assert result["required_return"] == 0.10
        assert result["required_return_pct"] == 10.0
        assert result["equity_risk_premium"] == 0.06

    def test_capm_zero_beta(self):
        from analyst.models import capm
        result = capm(risk_free_rate=0.04, market_return=0.10, beta=0.0)
        assert result["required_return"] == 0.04  # risk-free only

    def test_capm_high_beta(self):
        from analyst.models import capm
        result = capm(risk_free_rate=0.04, market_return=0.10, beta=2.0)
        assert result["required_return"] == pytest.approx(0.16)

    def test_capm_negative_beta_raises(self):
        from analyst.models import capm
        with pytest.raises(ValueError, match="non-negative"):
            capm(risk_free_rate=0.04, market_return=0.10, beta=-0.5)


# ── Compound Growth Rate ────────────────────────────────────────────────────


class TestCompoundGrowthRate:

    def test_basic_cagr(self):
        from analyst.models import compound_growth_rate
        result = compound_growth_rate(100.0, 200.0, 10.0)
        assert result["function"] == "compound_growth_rate"
        assert result["cagr"] == pytest.approx(0.071773, rel=1e-3)
        assert result["total_growth_pct"] == 100.0

    def test_no_growth(self):
        from analyst.models import compound_growth_rate
        result = compound_growth_rate(100.0, 100.0, 5.0)
        assert result["cagr"] == 0.0

    def test_negative_growth(self):
        from analyst.models import compound_growth_rate
        result = compound_growth_rate(100.0, 50.0, 5.0)
        assert result["cagr"] < 0
        assert result["total_growth_pct"] == -50.0

    def test_zero_beginning_raises(self):
        from analyst.models import compound_growth_rate
        with pytest.raises(ValueError, match="positive"):
            compound_growth_rate(0.0, 100.0, 5.0)

    def test_zero_years_raises(self):
        from analyst.models import compound_growth_rate
        with pytest.raises(ValueError, match="positive"):
            compound_growth_rate(100.0, 200.0, 0.0)


# ── DCF Valuation (explicit mode) ───────────────────────────────────────────


class TestDCFValuation:

    def test_explicit_dcf(self):
        """DCF with explicit parameters produces sensible intrinsic value."""
        from analyst.models import dcf_valuation
        result = dcf_valuation(
            fcf_per_share=10.0,
            growth_rate=0.10,
            terminal_growth=0.025,
            wacc=0.09,
            years=5,
        )
        assert result["function"] == "dcf_valuation"
        assert result["data_source"] == "explicit"
        assert result["intrinsic_value_per_share"] > 0
        assert result["dcf_steps"]["forecast_years"] == 5
        assert len(result["dcf_steps"]["stage1_cash_flows"]) == 5
        assert len(result["sensitivity_table"]) > 0

    def test_dcf_terminal_growth_ge_wacc_raises(self):
        """Terminal growth must be < WACC."""
        from analyst.models import dcf_valuation
        with pytest.raises(ValueError, match="Terminal growth"):
            dcf_valuation(
                fcf_per_share=10.0, growth_rate=0.10,
                terminal_growth=0.10, wacc=0.09,
            )

    def test_dcf_zero_years_raises(self):
        """Forecast period must be >= 1."""
        from analyst.models import dcf_valuation
        with pytest.raises(ValueError, match="years"):
            dcf_valuation(
                fcf_per_share=10.0, growth_rate=0.10,
                terminal_growth=0.025, wacc=0.09, years=0,
            )

    def test_dcf_missing_params_raises(self):
        """Missing required params without ticker raises ValueError."""
        from analyst.models import dcf_valuation
        with pytest.raises(ValueError):
            dcf_valuation(fcf_per_share=10.0)  # missing growth, wacc, tg


# ── Projected Income Statement (explicit mode) ──────────────────────────────


class TestProjectedIncomeStatement:

    def test_explicit_projection(self):
        from analyst.models import projected_income_statement
        result = projected_income_statement(
            revenue=100e9, growth_rate=0.08, opex_ratio=0.70, years=3,
        )
        assert result["function"] == "projected_income_statement"
        assert len(result["projection"]) == 3
        # Year 1 revenue should be ~108B
        assert result["projection"][0]["revenue"] == pytest.approx(108e9, rel=0.01)

    def test_missing_revenue_raises(self):
        from analyst.models import projected_income_statement
        with pytest.raises(ValueError, match="revenue"):
            projected_income_statement(growth_rate=0.08, opex_ratio=0.7)

    def test_opex_ratio_clamped(self):
        """Extreme opex ratios are clamped to [0.05, 0.95]."""
        from analyst.models import projected_income_statement
        result = projected_income_statement(
            revenue=100e9, growth_rate=0.05, opex_ratio=1.5, years=1,
        )
        assert result["assumptions"]["opex_ratio"] == 0.95


# ── Dividend Discount Model (explicit mode) ──────────────────────────────────


class TestDividendDiscountModel:

    def test_explicit_ddm(self):
        from analyst.models import dividend_discount_model
        result = dividend_discount_model(
            current_price=165.0,
            current_dividend=4.76,
            dividend_growth_rate=0.06,
            required_return=0.09,
        )
        assert result["function"] == "dividend_discount_model"
        assert result["intrinsic_value_per_share"] > 0
        # d1 = 4.76 * 1.06 = 5.0456; IV = 5.0456 / (0.09 - 0.06) = ~168.19
        assert result["intrinsic_value_per_share"] == pytest.approx(168.19, rel=0.01)

    def test_ddm_growth_ge_required_raises(self):
        """Growth >= required return is invalid."""
        from analyst.models import dividend_discount_model
        with pytest.raises(ValueError, match="less than"):
            dividend_discount_model(
                current_price=100.0, current_dividend=3.0,
                dividend_growth_rate=0.10, required_return=0.09,
            )

    def test_ddm_no_dividend_returns_error(self):
        """Non-dividend stocks return a structured error."""
        from analyst.models import dividend_discount_model
        result = dividend_discount_model(
            current_price=100.0, current_dividend=0.0,
            dividend_growth_rate=0.05, required_return=0.09,
        )
        assert "error" in result
        assert "not applicable" in result["error"].lower()

    def test_ddm_verdict(self):
        """Verdict correctly identifies under/overvalued."""
        from analyst.models import dividend_discount_model
        # Intrinsic >> price → undervalued
        result = dividend_discount_model(
            current_price=50.0, current_dividend=5.0,
            dividend_growth_rate=0.05, required_return=0.09,
        )
        assert result["verdict"] == "undervalued"


# ── Technical Summary (explicit mode) ────────────────────────────────────────


class TestTechnicalSummary:

    def test_explicit_technical_summary(self):
        """Technical summary computes without errors from a DataFrame."""
        from analyst.models import technical_summary
        # Generate synthetic price data (50 days)
        dates = pd.date_range("2025-01-01", periods=50, freq="B")
        np.random.seed(42)
        prices = 100 + np.cumsum(np.random.randn(50) * 2)
        df = pd.DataFrame({
            "Close": prices,
            "High": prices + 2,
            "Low": prices - 2,
            "Volume": np.random.randint(1_000_000, 10_000_000, 50),
        }, index=dates)
        result = technical_summary(prices_df=df)
        assert result["function"] == "technical_summary"
        assert result["latest"]["close"] is not None
        assert "signals" in result["latest"]

    def test_insufficient_data(self):
        """Too few data points returns an error."""
        from analyst.models import technical_summary
        df = pd.DataFrame({"Close": [100.0]})
        result = technical_summary(prices_df=df)
        assert "error" in result

    def test_missing_close_column_raises(self):
        """Missing 'Close' column raises ValueError."""
        from analyst.models import technical_summary
        df = pd.DataFrame({"Price": [100.0, 101.0]})
        with pytest.raises(ValueError, match="Close"):
            technical_summary(prices_df=df)


# ── Interpreter sandbox ──────────────────────────────────────────────────────


class TestInterpreter:

    def test_basic_execution(self):
        from analyst.interpreter import run_python
        result = run_python("print('hello')")
        assert result["success"] is True
        assert "hello" in result["stdout"]

    def test_result_variable(self):
        from analyst.interpreter import run_python
        result = run_python("_result = 42")
        assert result["success"] is True
        assert result["result"] == 42

    def test_blocked_import(self):
        """Importing os is blocked."""
        from analyst.interpreter import run_python
        result = run_python("import os")
        assert result["success"] is False
        assert "blocked" in result["error"].lower() or "not allowed" in result["error"].lower()

    def test_blocked_builtins(self):
        """open() is blocked."""
        from analyst.interpreter import run_python
        result = run_python("open('test.txt')")
        assert result["success"] is False

    def test_math_available(self):
        """math module is pre-loaded."""
        from analyst.interpreter import run_python
        result = run_python("_result = math.sqrt(16)")
        assert result["success"] is True
        assert result["result"] == 4.0

    def test_allowed_import(self):
        """numpy is allowed when requested."""
        from analyst.interpreter import run_python
        result = run_python("_result = numpy.array([1,2,3]).sum()", imports=["numpy"])
        assert result["success"] is True
        assert result["result"] == 6

    def test_disallowed_extra_import(self):
        """Requesting a blocked module returns error before execution."""
        from analyst.interpreter import run_python
        result = run_python("pass", imports=["subprocess"])
        assert result["success"] is False
        assert "not allowed" in result["error"].lower()

    def test_execution_error_captured(self):
        """Runtime errors are captured, not raised."""
        from analyst.interpreter import run_python
        result = run_python("1/0")
        assert result["success"] is False
        assert "ZeroDivisionError" in result["error"]
        assert result["traceback"] is not None
