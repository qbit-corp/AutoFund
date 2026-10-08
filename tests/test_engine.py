"""
Tests for the Paper Trading Engine (trader/engine.py).
Covers: buy, sell, portfolio, reset, persistence, conditions, edge cases.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from trader.engine import PaperTrader, TradeError


# ── Initialization & Persistence ─────────────────────────────────────────────


class TestPaperTraderInit:

    def test_creates_new_portfolio_file(self, tmp_portfolio):
        """New trader creates a portfolio JSON on disk."""
        assert not tmp_portfolio.exists()
        pt = PaperTrader(portfolio_path=tmp_portfolio, initial_capital=50_000.0)
        assert tmp_portfolio.exists()
        data = json.loads(tmp_portfolio.read_text())
        assert data["cash"] == 50_000.0
        assert data["initial_capital"] == 50_000.0
        assert data["positions"] == {}
        assert data["transactions"] == []
        assert data["pending_sells"] == []

    def test_loads_existing_portfolio(self, tmp_portfolio):
        """Loading an existing portfolio file restores state."""
        state = {
            "created_at": "2026-01-01T00:00:00Z",
            "initial_capital": 75_000.0,
            "cash": 60_000.0,
            "positions": {"AAPL": {"shares": 10, "avg_cost": 150.0, "total_cost": 1500.0}},
            "transactions": [{"id": "txn_1"}],
            "pending_sells": [],
        }
        tmp_portfolio.write_text(json.dumps(state))
        pt = PaperTrader(portfolio_path=tmp_portfolio)
        assert pt._state["cash"] == 60_000.0
        assert "AAPL" in pt._state["positions"]

    def test_default_initial_capital(self, tmp_portfolio):
        """Default capital is 100k."""
        pt = PaperTrader(portfolio_path=tmp_portfolio)
        assert pt._state["initial_capital"] == 100_000.0

    def test_pending_sells_migration(self, tmp_portfolio):
        """Old portfolios without pending_sells get the field added."""
        state = {
            "created_at": "2025-01-01T00:00:00Z",
            "initial_capital": 100_000.0,
            "cash": 100_000.0,
            "positions": {},
            "transactions": [],
            # no "pending_sells" key
        }
        tmp_portfolio.write_text(json.dumps(state))
        pt = PaperTrader(portfolio_path=tmp_portfolio)
        assert "pending_sells" in pt._state

    def test_atomic_save(self, tmp_portfolio):
        """_save writes via temp file and replaces atomically."""
        pt = PaperTrader(portfolio_path=tmp_portfolio)
        pt._state["cash"] = 42.0
        pt._save()
        data = json.loads(tmp_portfolio.read_text())
        assert data["cash"] == 42.0
        # No .tmp file should remain
        assert not tmp_portfolio.with_suffix(".tmp").exists()


# ── Buy ──────────────────────────────────────────────────────────────────────


class TestBuy:

    def test_basic_buy(self, fresh_trader):
        """Buy 10 shares and verify cash deduction and position creation."""
        pt = fresh_trader
        with patch.object(pt, "_get_live_price", return_value=100.0), \
             patch("trader.engine.validate_ticker", return_value=(True, "OK")), \
             patch("trader.engine.is_market_open", return_value=(True, "open")):
            txn = pt.buy("AAPL", 10, reason="Test")
        assert txn["type"] == "BUY"
        assert txn["symbol"] == "AAPL"
        assert txn["shares"] == 10
        assert pt._state["cash"] == 99_000.0
        pos = pt._state["positions"]["AAPL"]
        assert pos["shares"] == 10
        assert pos["avg_cost"] == 100.0
        assert pos["peak_price"] == 100.0

    def test_buy_force_skips_market_check(self, fresh_trader):
        """force=True should skip market-hours check."""
        pt = fresh_trader
        with patch.object(pt, "_get_live_price", return_value=50.0), \
             patch("trader.engine.validate_ticker", return_value=(True, "OK")):
            txn = pt.buy("AAPL", 5, force=True)
        assert txn["total"] == 250.0

    def test_buy_accumulates_position(self, trader_with_position):
        """Buying more shares averages the cost."""
        pt = trader_with_position
        # Already holds 10 shares at $100; buy 10 more at $200
        with patch.object(pt, "_get_live_price", return_value=200.0), \
             patch("trader.engine.validate_ticker", return_value=(True, "OK")):
            pt.buy("AAPL", 10, force=True)
        pos = pt._state["positions"]["AAPL"]
        assert pos["shares"] == 20
        assert pos["total_cost"] == 3000.0  # 1000 + 2000
        assert pos["avg_cost"] == 150.0     # 3000 / 20

    def test_buy_accumulation_updates_peak_price(self, trader_with_position):
        """Buying more shares at a higher price updates peak_price for trailing stops."""
        pt = trader_with_position
        assert pt._state["positions"]["AAPL"]["peak_price"] == 100.0
        # Buy more at $200 — peak should update to 200
        with patch.object(pt, "_get_live_price", return_value=200.0), \
             patch("trader.engine.validate_ticker", return_value=(True, "OK")):
            pt.buy("AAPL", 5, force=True)
        assert pt._state["positions"]["AAPL"]["peak_price"] == 200.0

    def test_buy_accumulation_keeps_peak_when_lower(self, trader_with_position):
        """Buying more shares at a lower price does NOT lower peak_price."""
        pt = trader_with_position
        assert pt._state["positions"]["AAPL"]["peak_price"] == 100.0
        # Buy more at $80 — peak should stay at 100
        with patch.object(pt, "_get_live_price", return_value=80.0), \
             patch("trader.engine.validate_ticker", return_value=(True, "OK")):
            pt.buy("AAPL", 5, force=True)
        assert pt._state["positions"]["AAPL"]["peak_price"] == 100.0

    def test_buy_with_sell_conditions(self, fresh_trader):
        """Sell conditions are stored on buy."""
        pt = fresh_trader
        conditions = [
            {"type": "price_target", "target": 200.0, "operator": "gte", "reason": "TP"},
            {"type": "pnl_pct", "target": -5.0, "operator": "lte", "reason": "SL"},
        ]
        with patch.object(pt, "_get_live_price", return_value=100.0), \
             patch("trader.engine.validate_ticker", return_value=(True, "OK")):
            pt.buy("AAPL", 5, sell_conditions=conditions, force=True)
        conds = pt._state["positions"]["AAPL"]["sell_conditions"]
        assert len(conds) == 2
        assert all(c["condition_id"].startswith("cond_") for c in conds)
        assert all(c["triggered"] is False for c in conds)
        assert conds[0]["target"] == 200.0
        assert conds[1]["target"] == -5.0

    def test_buy_with_fundamental_condition_preserves_metric(self, fresh_trader):
        """Fundamental sell condition on buy() must preserve the 'metric' field."""
        pt = fresh_trader
        conditions = [
            {"type": "fundamental", "target": 30.0, "operator": "gt",
             "reason": "P/E too high", "metric": "trailingPE"},
        ]
        with patch.object(pt, "_get_live_price", return_value=100.0), \
             patch("trader.engine.validate_ticker", return_value=(True, "OK")):
            pt.buy("AAPL", 5, sell_conditions=conditions, force=True)
        conds = pt._state["positions"]["AAPL"]["sell_conditions"]
        assert len(conds) == 1
        assert conds[0]["metric"] == "trailingPE"

    def test_buy_zero_quantity_raises(self, fresh_trader):
        """Quantity must be > 0."""
        with pytest.raises(TradeError, match="positive integer"):
            fresh_trader.buy("AAPL", 0, force=True)

    def test_buy_negative_quantity_raises(self, fresh_trader):
        """Negative quantity is rejected."""
        with pytest.raises(TradeError, match="positive integer"):
            fresh_trader.buy("AAPL", -5, force=True)

    def test_buy_insufficient_cash_raises(self, fresh_trader):
        """Buying more than available cash is rejected."""
        pt = fresh_trader
        with patch.object(pt, "_get_live_price", return_value=200_000.0), \
             patch("trader.engine.validate_ticker", return_value=(True, "OK")):
            with pytest.raises(TradeError, match="Insufficient cash"):
                pt.buy("AAPL", 1, force=True)

    def test_buy_invalid_ticker_raises(self, fresh_trader):
        """Invalid ticker is rejected."""
        pt = fresh_trader
        with patch("trader.engine.validate_ticker", return_value=(False, "bad ticker")):
            with pytest.raises(TradeError, match="rejected"):
                pt.buy("XXXXX", 1, force=True)

    def test_buy_market_closed_raises(self, fresh_trader):
        """Market closed rejects trade (without force)."""
        pt = fresh_trader
        with patch("trader.engine.validate_ticker", return_value=(True, "OK")), \
             patch("trader.engine.is_market_open", return_value=(False, "closed")):
            with pytest.raises(TradeError, match="Cannot trade"):
                pt.buy("AAPL", 1)

    def test_buy_normalises_symbol(self, fresh_trader):
        """Symbol is uppercased and stripped."""
        pt = fresh_trader
        with patch.object(pt, "_get_live_price", return_value=100.0), \
             patch("trader.engine.validate_ticker", return_value=(True, "OK")):
            pt.buy("  aapl  ", 1, force=True)
        assert "AAPL" in pt._state["positions"]


# ── Sell ─────────────────────────────────────────────────────────────────────


class TestSell:

    def test_basic_sell(self, trader_with_position):
        """Full sell liquidates position and credits cash."""
        pt = trader_with_position
        with patch.object(pt, "_get_live_price", return_value=120.0), \
             patch("trader.engine.validate_ticker", return_value=(True, "OK")):
            txn = pt.sell("AAPL", 10, reason="Take profit", force=True)
        assert txn["type"] == "SELL"
        assert txn["shares"] == 10
        assert "AAPL" not in pt._state["positions"]
        assert pt._state["cash"] == 99_000.0 + 1200.0

    def test_partial_sell(self, trader_with_position):
        """Partial sell reduces shares and adjusts total_cost."""
        pt = trader_with_position
        with patch.object(pt, "_get_live_price", return_value=120.0), \
             patch("trader.engine.validate_ticker", return_value=(True, "OK")):
            pt.sell("AAPL", 5, force=True)
        pos = pt._state["positions"]["AAPL"]
        assert pos["shares"] == 5
        # total_cost = avg_cost * remaining_shares = 100 * 5 = 500
        assert pos["total_cost"] == 500.0

    def test_sell_nonexistent_position_raises(self, fresh_trader):
        """Cannot sell what you don't own."""
        with pytest.raises(TradeError, match="No position"):
            fresh_trader.sell("AAPL", 1, force=True)

    def test_sell_more_than_owned_raises(self, trader_with_position):
        """Cannot sell more shares than held."""
        pt = trader_with_position
        with patch("trader.engine.validate_ticker", return_value=(True, "OK")):
            with pytest.raises(TradeError, match="Insufficient shares"):
                pt.sell("AAPL", 20, force=True)

    def test_sell_records_transaction(self, trader_with_position):
        """Sell records a transaction in history."""
        pt = trader_with_position
        initial_txn_count = len(pt._state["transactions"])
        with patch.object(pt, "_get_live_price", return_value=110.0), \
             patch("trader.engine.validate_ticker", return_value=(True, "OK")):
            pt.sell("AAPL", 5, reason="Rebalance", force=True)
        assert len(pt._state["transactions"]) == initial_txn_count + 1
        txn = pt._state["transactions"][-1]
        assert txn["reason"] == "Rebalance"


# ── Portfolio ────────────────────────────────────────────────────────────────


class TestPortfolio:

    def test_empty_portfolio(self, fresh_trader):
        """Empty portfolio returns correct structure."""
        pt = fresh_trader
        p = pt.get_portfolio()
        assert p["cash"] == 100_000.0
        assert p["positions"] == []
        assert p["total_value"] == 100_000.0
        assert p["total_pnl"] == 0.0

    def test_portfolio_with_position(self, trader_with_position):
        """Portfolio includes position with live valuations."""
        pt = trader_with_position
        with patch.object(pt, "_get_live_price", return_value=120.0):
            p = pt.get_portfolio()
        assert len(p["positions"]) == 1
        pos = p["positions"][0]
        assert pos["symbol"] == "AAPL"
        assert pos["shares"] == 10
        assert pos["current_price"] == 120.0
        assert pos["market_value"] == 1200.0
        assert pos["unrealized_pnl"] == 200.0  # 1200 - 1000
        assert pos["unrealized_pnl_pct"] == 20.0

    def test_portfolio_weight_calculation(self, trader_with_position):
        """Position weight is computed as % of total portfolio."""
        pt = trader_with_position
        with patch.object(pt, "_get_live_price", return_value=100.0):
            p = pt.get_portfolio()
        pos = p["positions"][0]
        # market_value = 1000, total_value = 99000 + 1000 = 100000
        assert pos["weight_pct"] == 1.0  # 1000 / 100000 * 100

    def test_portfolio_pnl_fallback(self, trader_with_position):
        """When price fetch fails, fall back to avg_cost."""
        pt = trader_with_position
        with patch.object(pt, "_get_live_price", side_effect=TradeError("no data")):
            p = pt.get_portfolio()
        pos = p["positions"][0]
        assert pos["current_price"] == 100.0  # fallback to avg_cost
        assert pos["unrealized_pnl"] == 0.0


# ── Reset ────────────────────────────────────────────────────────────────────


class TestReset:

    def test_reset_clears_everything(self, trader_with_position):
        """Reset wipes positions, transactions, and restores cash."""
        pt = trader_with_position
        pt.reset()
        assert pt._state["cash"] == 100_000.0
        assert pt._state["positions"] == {}
        assert pt._state["transactions"] == []
        assert pt._state["pending_sells"] == []

    def test_reset_with_custom_capital(self, trader_with_position):
        """Reset with custom capital."""
        pt = trader_with_position
        pt.reset(initial_capital=50_000.0)
        assert pt._state["cash"] == 50_000.0
        assert pt._state["initial_capital"] == 50_000.0


# ── Transaction History ──────────────────────────────────────────────────────


class TestTransactionHistory:

    def test_history_ordering(self, fresh_trader):
        """Transactions are returned newest-first."""
        pt = fresh_trader
        for i in range(5):
            with patch.object(pt, "_get_live_price", return_value=100.0 + i), \
                 patch("trader.engine.validate_ticker", return_value=(True, "OK")):
                pt.buy("AAPL", 1, reason=f"buy_{i}", force=True)
        history = pt.get_transaction_history(limit=5)
        # newest-first → last buy should be first in result
        assert history[0]["reason"] == "buy_4"
        assert history[-1]["reason"] == "buy_0"

    def test_history_limit(self, fresh_trader):
        """Limit caps the number of returned transactions."""
        pt = fresh_trader
        for _ in range(10):
            with patch.object(pt, "_get_live_price", return_value=100.0), \
                 patch("trader.engine.validate_ticker", return_value=(True, "OK")):
                pt.buy("AAPL", 1, force=True)
        assert len(pt.get_transaction_history(limit=3)) == 3


# ── Add Sell Condition ───────────────────────────────────────────────────────


class TestAddSellCondition:

    def test_add_condition_to_position(self, trader_with_position):
        """add_sell_condition appends to existing position."""
        pt = trader_with_position
        cond = pt.add_sell_condition("AAPL", {
            "type": "price_target", "target": 200, "operator": "gte", "reason": "TP"
        })
        assert cond is not None
        assert cond["condition_id"].startswith("cond_")
        assert cond["target"] == 200.0
        assert cond["triggered"] is False
        assert len(pt._state["positions"]["AAPL"]["sell_conditions"]) == 1

    def test_add_condition_nonexistent_position(self, fresh_trader):
        """Returns None for a position that doesn't exist."""
        result = fresh_trader.add_sell_condition("XXXX", {
            "type": "price_target", "target": 100, "operator": "gte"
        })
        assert result is None

    def test_add_fundamental_condition(self, trader_with_position):
        """Fundamental condition includes metric field."""
        pt = trader_with_position
        cond = pt.add_sell_condition("AAPL", {
            "type": "fundamental", "target": 35.0, "operator": "gt",
            "reason": "P/E too high", "metric": "trailingPE",
        })
        assert cond["metric"] == "trailingPE"


# ── Get Position ─────────────────────────────────────────────────────────────


class TestGetPosition:

    def test_get_existing_position(self, trader_with_position):
        """Returns details for a held position."""
        pt = trader_with_position
        with patch.object(pt, "_get_live_price", return_value=110.0):
            pos = pt.get_position("AAPL")
        assert pos is not None
        assert pos["symbol"] == "AAPL"
        assert pos["current_price"] == 110.0
        assert pos["unrealized_pnl"] == 100.0  # (110 - 100) * 10

    def test_get_nonexistent_position(self, fresh_trader):
        """Returns None for a ticker not in portfolio."""
        assert fresh_trader.get_position("MSFT") is None


# ── Check Sell Conditions (integration) ──────────────────────────────────────


class TestCheckSellConditions:

    def test_price_target_triggers_sell(self, trader_with_conditions):
        """When price >= target, the condition triggers."""
        pt = trader_with_conditions
        with patch.object(pt, "_get_live_price", return_value=160.0), \
             patch.object(pt, "_is_market_open_for_symbol", return_value=(True, "open")):
            result = pt.check_sell_conditions()
        triggered = result["conditions_triggered"]
        assert len(triggered) >= 1
        price_triggered = [t for t in triggered if t["type"] == "price_target"]
        assert len(price_triggered) == 1
        # Should have attempted to sell
        assert price_triggered[0]["sell_executed"] is True

    def test_stop_loss_triggers(self, trader_with_conditions):
        """When PnL% drops to/below stop, condition triggers."""
        pt = trader_with_conditions
        # avg_cost=100, price=89 → pnl = -11%
        with patch.object(pt, "_get_live_price", return_value=89.0), \
             patch.object(pt, "_is_market_open_for_symbol", return_value=(True, "open")):
            result = pt.check_sell_conditions()
        triggered = result["conditions_triggered"]
        stop_triggered = [t for t in triggered if t["type"] == "pnl_pct"]
        assert len(stop_triggered) == 1

    def test_no_conditions_no_check(self, trader_with_position):
        """Positions without sell conditions are skipped."""
        pt = trader_with_position
        with patch.object(pt, "_get_live_price", return_value=150.0):
            result = pt.check_sell_conditions()
        assert result["positions_checked"] == 0

    def test_market_closed_queues_pending_sell(self, trader_with_conditions):
        """When market is closed, triggered sell is queued."""
        pt = trader_with_conditions
        with patch.object(pt, "_get_live_price", return_value=160.0), \
             patch.object(pt, "_is_market_open_for_symbol", return_value=(False, "closed")):
            result = pt.check_sell_conditions()
        triggered = result["conditions_triggered"]
        price_triggered = [t for t in triggered if t["type"] == "price_target"]
        assert len(price_triggered) == 1
        assert price_triggered[0]["sell_executed"] is False
        assert "closed" in price_triggered[0]["sell_error"]
        # Should be in pending_sells
        assert len(pt._state["pending_sells"]) >= 1

    def test_pending_sell_flushed_when_market_opens(self, trader_with_conditions):
        """Pending sells are executed when their market opens."""
        pt = trader_with_conditions
        # Queue a pending sell
        pt._state["pending_sells"] = [{
            "symbol": "AAPL",
            "condition_id": "cond_price",
            "condition_type": "price_target",
            "target": 150.0,
            "operator": "gte",
            "reason": "Triggered: price_target gte 150.0",
            "queued_at": "2026-01-01T00:00:00Z",
        }]
        pt._save()
        # Now market is open; check_sell_conditions should flush pending
        with patch.object(pt, "_get_live_price", return_value=160.0), \
             patch.object(pt, "_is_market_open_for_symbol", return_value=(True, "open")), \
             patch("trader.engine.validate_ticker", return_value=(True, "OK")):
            pt.check_sell_conditions()
        # Position should be gone (sold) and pending cleared
        assert "AAPL" not in pt._state["positions"]
        assert len(pt._state["pending_sells"]) == 0

    def test_auto_sell_failure_queues_pending(self, trader_with_conditions):
        """When auto-sell fails due to a transient error, sell is queued for retry."""
        pt = trader_with_conditions
        # Make sell() raise an exception on the first call
        with patch.object(pt, "_get_live_price", return_value=160.0), \
             patch.object(pt, "_is_market_open_for_symbol", return_value=(True, "open")), \
             patch("trader.engine.validate_ticker", side_effect=Exception("Network error")):
            result = pt.check_sell_conditions()
        triggered = result["conditions_triggered"]
        price_triggered = [t for t in triggered if t["type"] == "price_target"]
        assert len(price_triggered) == 1
        # Sell should have failed
        assert price_triggered[0]["sell_executed"] is False
        assert price_triggered[0]["sell_error"] is not None
        # But it should be queued as pending so it retries
        assert len(pt._state["pending_sells"]) >= 1
        assert pt._state["pending_sells"][0]["symbol"] == "AAPL"

    def test_pending_sells_deduplicated_per_symbol(self, fresh_trader):
        """Multiple conditions triggering for same symbol only queue one pending sell."""
        pt = fresh_trader
        pt._state["positions"] = {
            "AAPL": {
                "symbol": "AAPL",
                "shares": 10,
                "avg_cost": 100.0,
                "total_cost": 1000.0,
                "peak_price": 200.0,
                "sell_conditions": [
                    {"condition_id": "c1", "type": "price_target", "operator": "gte",
                     "target": 150.0, "reason": "", "triggered": False, "triggered_at": None},
                    {"condition_id": "c2", "type": "pnl_pct", "operator": "gte",
                     "target": 50.0, "reason": "", "triggered": False, "triggered_at": None},
                ],
            },
        }
        pt._state["cash"] = 99000.0
        pt._save()
        # Both conditions trigger, market is closed → should only queue ONE pending sell
        with patch.object(pt, "_get_live_price", return_value=160.0), \
             patch.object(pt, "_is_market_open_for_symbol", return_value=(False, "closed")):
            result = pt.check_sell_conditions()
        assert len(result["conditions_triggered"]) == 2
        # Only one pending sell should be queued for AAPL
        aapl_pending = [ps for ps in pt._state["pending_sells"] if ps["symbol"] == "AAPL"]
        assert len(aapl_pending) == 1


# ── Condition Status ─────────────────────────────────────────────────────────


class TestConditionStatus:

    def test_condition_status_returns_details(self, trader_with_conditions):
        """get_condition_status returns per-condition breakdown."""
        pt = trader_with_conditions
        with patch.object(pt, "_get_live_price", return_value=110.0):
            status = pt.get_condition_status("AAPL")
        assert status is not None
        assert status["symbol"] == "AAPL"
        assert status["condition_summary"]["total"] == 2
        assert len(status["conditions"]) == 2

    def test_condition_status_nonexistent(self, fresh_trader):
        """Returns None for a symbol not held."""
        assert fresh_trader.get_condition_status("XXXX") is None
