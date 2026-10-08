"""
Tests for the agent tools module (agents/tools.py).
Covers: tool registry, role-to-tool mapping, execute_trade, read/write file,
        portfolio tool, and tool execution dispatch.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agents.tools import (
    TOOL_REGISTRY,
    get_tools_for_role,
    execute_tool_call,
    _execute_trade,
    _read_file,
    _write_file,
    _get_portfolio,
    _check_sell_conditions,
    PROJECT_ROOT,
)


# ── Tool Registry ────────────────────────────────────────────────────────────


class TestToolRegistry:

    def test_all_tools_have_schema_and_handler(self):
        """Every registered tool must have both schema and handler."""
        for name, entry in TOOL_REGISTRY.items():
            assert "schema" in entry, f"Tool '{name}' missing 'schema'"
            assert "handler" in entry, f"Tool '{name}' missing 'handler'"
            assert callable(entry["handler"]), f"Tool '{name}' handler is not callable"

    def test_schema_has_function_name(self):
        """Schema function name must match registry key."""
        for name, entry in TOOL_REGISTRY.items():
            schema_name = entry["schema"]["function"]["name"]
            assert schema_name == name, (
                f"Registry key '{name}' doesn't match schema name '{schema_name}'"
            )

    def test_expected_tools_registered(self):
        """All expected tool names are present."""
        expected = {
            "run_scraper", "pull_market_data", "run_model", "run_python",
            "execute_trade", "get_portfolio", "check_sell_conditions",
            "read_file", "write_file",
        }
        assert expected.issubset(set(TOOL_REGISTRY.keys()))


# ── Role → Tool Mapping ─────────────────────────────────────────────────────


class TestRoleToolMapping:

    @pytest.mark.parametrize("role,expected_tools", [
        ("economist", []),
        ("researcher", ["read_file", "write_file"]),
        ("analyst", ["pull_market_data", "run_model", "run_python"]),
        ("associates", []),
        ("manager", ["execute_trade", "get_portfolio", "check_sell_conditions", "read_file", "write_file"]),
    ])
    def test_role_gets_correct_tools(self, role, expected_tools):
        """Each role gets exactly the right set of tools."""
        schemas = get_tools_for_role(role)
        tool_names = [s["function"]["name"] for s in schemas]
        assert tool_names == expected_tools

    def test_unknown_role_returns_empty(self):
        """An unrecognized role returns no tools."""
        assert get_tools_for_role("janitor") == []


# ── execute_tool_call dispatch ───────────────────────────────────────────────


class TestExecuteToolCall:

    def test_unknown_tool_returns_error(self):
        """Calling a non-existent tool returns an error dict."""
        result = execute_tool_call("nonexistent_tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    def test_handler_exception_caught(self):
        """Exceptions in tool handlers are caught and returned as errors."""
        with patch.dict(TOOL_REGISTRY, {
            "failing_tool": {
                "schema": {"function": {"name": "failing_tool"}},
                "handler": lambda args: (_ for _ in ()).throw(ValueError("boom")),
            }
        }):
            result = execute_tool_call("failing_tool", {})
            assert "error" in result
            assert "ValueError" in result["error"]


# ── _execute_trade ───────────────────────────────────────────────────────────


class TestExecuteTrade:

    def test_invalid_quantity(self):
        """Malformed quantity like '10 shares' returns an error."""
        result = _execute_trade({"action": "BUY", "symbol": "AAPL", "quantity": "ten shares"})
        assert result["success"] is False
        assert "Invalid quantity" in result["error"]

    def test_none_quantity(self):
        """None quantity is handled."""
        result = _execute_trade({"action": "BUY", "symbol": "AAPL", "quantity": None})
        assert result["success"] is False
        assert "Invalid quantity" in result["error"]

    def test_float_quantity_truncated(self):
        """Float quantity like 10.5 is truncated to int."""
        with patch("agents.tools._get_paper_trader") as mock_pt:
            mock_trader = MagicMock()
            mock_trader.buy.return_value = {"id": "txn_1"}
            mock_pt.return_value = mock_trader
            result = _execute_trade({
                "action": "BUY", "symbol": "AAPL", "quantity": 10.5,
                "reason": "test", "force": True,
            })
        # Should call buy with quantity=10
        mock_trader.buy.assert_called_once()
        call_args = mock_trader.buy.call_args
        assert call_args[0][1] == 10  # quantity argument

    def test_unknown_action(self):
        """Unknown action returns error."""
        with patch("agents.tools._get_paper_trader") as mock_pt:
            mock_pt.return_value = MagicMock()
            result = _execute_trade({"action": "HOLD", "symbol": "AAPL", "quantity": 1})
        assert "error" in result
        assert "Unknown trade action" in result["error"]


# ── _read_file / _write_file ────────────────────────────────────────────────


class TestFileTools:

    def test_read_existing_file(self):
        """Reading an existing file returns its content."""
        result = _read_file({"path": "requirements.txt"})
        assert "content" in result
        assert "yfinance" in result["content"]

    def test_read_nonexistent_file(self):
        """Reading a missing file returns error."""
        result = _read_file({"path": "nonexistent_file_xyz.txt"})
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_path_traversal_blocked_read(self):
        """Path traversal via ../ is blocked."""
        result = _read_file({"path": "../../etc/passwd"})
        assert "error" in result
        assert "Access denied" in result["error"]

    def test_path_traversal_blocked_write(self):
        """Path traversal on write is blocked."""
        result = _write_file({"path": "../../evil.txt", "content": "pwned"})
        assert "error" in result
        assert "Access denied" in result["error"]

    def test_write_and_read(self, tmp_path, monkeypatch):
        """Write a file then read it back."""
        # Temporarily point PROJECT_ROOT to tmp_path
        import agents.tools as tools_module
        original_root = tools_module.PROJECT_ROOT
        monkeypatch.setattr(tools_module, "PROJECT_ROOT", tmp_path)
        try:
            write_result = _write_file({"path": "test_output.txt", "content": "hello"})
            assert write_result["success"] is True
            read_result = _read_file({"path": "test_output.txt"})
            assert read_result["content"] == "hello"
        finally:
            monkeypatch.setattr(tools_module, "PROJECT_ROOT", original_root)
