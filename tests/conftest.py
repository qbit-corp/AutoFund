"""
Shared fixtures for the AutoFund test suite.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def tmp_portfolio(tmp_path):
    """Return a path for a temporary portfolio JSON file."""
    return tmp_path / "test_portfolio.json"


@pytest.fixture
def fresh_trader(tmp_portfolio):
    """Create a PaperTrader with an isolated temp portfolio."""
    from trader.engine import PaperTrader
    pt = PaperTrader(portfolio_path=tmp_portfolio, initial_capital=100_000.0)
    return pt


@pytest.fixture
def trader_with_position(fresh_trader):
    """
    Create a PaperTrader that already holds 10 shares of AAPL at $100.
    All network calls are mocked.
    """
    pt = fresh_trader
    pt._state["positions"] = {
        "AAPL": {
            "symbol": "AAPL",
            "shares": 10,
            "avg_cost": 100.0,
            "total_cost": 1000.0,
            "buy_reason": "Test buy",
            "buy_date": "2026-01-01T00:00:00Z",
            "peak_price": 100.0,
            "sell_conditions": [],
        }
    }
    pt._state["cash"] = 99_000.0
    pt._save()
    return pt


@pytest.fixture
def trader_with_conditions(trader_with_position):
    """Trader holding AAPL with two sell conditions attached."""
    pt = trader_with_position
    pt._state["positions"]["AAPL"]["sell_conditions"] = [
        {
            "condition_id": "cond_price",
            "type": "price_target",
            "operator": "gte",
            "target": 150.0,
            "reason": "Take profit at $150",
            "created_at": "2026-01-01T00:00:00Z",
            "triggered": False,
            "triggered_at": None,
        },
        {
            "condition_id": "cond_stop",
            "type": "pnl_pct",
            "operator": "lte",
            "target": -10.0,
            "reason": "Stop loss at -10%",
            "created_at": "2026-01-01T00:00:00Z",
            "triggered": False,
            "triggered_at": None,
        },
    ]
    pt._save()
    return pt
