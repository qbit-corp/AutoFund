"""
AutoFund — Tool Definitions & Execution
=========================================
Defines the tools available to each agent and handles tool call execution.
Tools are registered as OpenAI-compatible function schemas and backed by
real Python implementations.
"""

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("agents.tools")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Tool execution implementations ───────────────────────────────────────────

def _run_scraper(args: dict) -> dict:
    """Run the Yahoo Finance headline scraper."""
    max_articles = args.get("max_articles", 15)
    output_path = args.get("output_path", None)

    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scraper" / "yfinance_scraper.py"),
        "--max", str(max_articles),
    ]
    if output_path:
        out_check = (PROJECT_ROOT / output_path).resolve()
        if not out_check.is_relative_to(PROJECT_ROOT.resolve()):
            return {"success": False, "error": f"Access denied: path '{output_path}' is outside the project root."}
        cmd += ["-o", output_path]

    log.info("Running scraper: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=1200
    )

    if result.returncode != 0:
        return {"success": False, "error": result.stderr[:2000]}

    # Find the output file
    if output_path:
        out = Path(output_path)
    else:
        # Find the most recently created json file
        jsons = sorted(PROJECT_ROOT.glob("yahoo_finance_articles_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = jsons[0] if jsons else None

    if out and out.exists():
        data = json.loads(out.read_text(encoding="utf-8"))
        return {"success": True, "output_path": str(out), "data": data}

    return {"success": True, "stdout": result.stdout[:5000]}


def _pull_market_data(args: dict) -> dict:
    """Pull market data via analyst/market_data.py."""
    from analyst.market_data import pull_ticker_data
    symbol = args.get("symbol", args.get("ticker", ""))
    modules = args.get("modules", None)
    period = args.get("period", "1y")
    interval = args.get("interval", "1d")
    return pull_ticker_data(symbol, modules=modules, period=period, interval=interval)


def _run_models(args: dict) -> dict:
    """Run a financial model from analyst/models.py."""
    from analyst import models
    import inspect
    func_name = args.get("function", "")
    params = args.get("parameters", {})
    
    if not isinstance(func_name, str) or func_name.startswith("_"):
        return {"error": f"Invalid model function name: {func_name}"}

    func = getattr(models, func_name, None)
    if func is None or not callable(func) or getattr(func, "__module__", "") != "analyst.models":
        return {"error": f"Unknown model function: {func_name}"}

    try:
        result = func(**params)
        return result
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _run_python(args: dict) -> dict:
    """Execute sandboxed Python code via the code interpreter."""
    from analyst.interpreter import run_python
    code = args.get("code", "")
    imports = args.get("imports", [])
    return run_python(code, imports=imports)


_paper_trader_instance: "PaperTrader | None" = None


def _get_paper_trader():
    """Return a shared PaperTrader instance (lazy singleton)."""
    global _paper_trader_instance
    if _paper_trader_instance is None:
        from trader import PaperTrader
        _paper_trader_instance = PaperTrader()
    return _paper_trader_instance


def _execute_trade(args: dict) -> dict:
    """Execute a buy or sell via the PaperTrader."""
    from trader import TradeError
    pt = _get_paper_trader()

    action = args.get("action", "").upper()
    symbol = args.get("symbol", "")
    reason = args.get("reason", "Pipeline decision")
    sell_conditions = args.get("sell_conditions", None)
    force = args.get("force", False)

    # Guard against malformed LLM output (e.g. "10 shares", 10.5, null)
    try:
        quantity = int(float(args.get("quantity", 0)))
    except (ValueError, TypeError):
        return {"success": False, "error": f"Invalid quantity: {args.get('quantity')!r}. Must be an integer."}

    try:
        if action == "BUY":
            result = pt.buy(
                symbol, quantity, reason=reason,
                sell_conditions=sell_conditions, force=force,
            )
        elif action == "SELL":
            result = pt.sell(symbol, quantity, reason=reason, force=force)
        else:
            return {"error": f"Unknown trade action: {action}"}
        return {"success": True, "transaction": result}
    except TradeError as e:
        return {"success": False, "error": str(e)}


def _get_portfolio(args: dict) -> dict:
    """Return current portfolio state."""
    pt = _get_paper_trader()
    return pt.get_portfolio()


def _check_sell_conditions(args: dict) -> dict:
    """Evaluate all sell conditions on open positions."""
    pt = _get_paper_trader()
    return pt.check_sell_conditions()



# ── Tool registry ─────────────────────────────────────────────────────────────

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "run_scraper": {
        "schema": {
            "type": "function",
            "function": {
                "name": "run_scraper",
                "description": "Run the Yahoo Finance headline and article scraper. Returns structured JSON with scraped articles.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "max_articles": {
                            "type": "integer",
                            "description": "Maximum number of articles to scrape (default: 15).",
                        },
                        "output_path": {
                            "type": "string",
                            "description": "Output JSON file path. Optional.",
                        },
                    },
                },
            },
        },
        "handler": _run_scraper,
    },
    "pull_market_data": {
        "schema": {
            "type": "function",
            "function": {
                "name": "pull_market_data",
                "description": "Pull structured market data from Yahoo Finance for a ticker. Modules: snapshot, history, financials, analysis, technicals, holders.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Ticker symbol (e.g. AAPL, MSFT, GC=F).",
                        },
                        "modules": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Data modules to pull. Default: all.",
                        },
                        "period": {
                            "type": "string",
                            "description": "History period (default: 1y).",
                        },
                        "interval": {
                            "type": "string",
                            "description": "History interval (default: 1d).",
                        },
                    },
                    "required": ["symbol"],
                },
            },
        },
        "handler": _pull_market_data,
    },
    "run_model": {
        "schema": {
            "type": "function",
            "function": {
                "name": "run_model",
                "description": "Run a financial model from the analyst/models.py library. Functions: dcf_valuation, projected_income_statement, technical_summary, peer_valuation_multiples, scenario_model, capm, dividend_discount_model, compound_growth_rate.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "function": {
                            "type": "string",
                            "description": "Model function name.",
                        },
                        "parameters": {
                            "type": "object",
                            "description": "Parameters to pass to the function. Use ticker-based pattern when possible.",
                        },
                    },
                    "required": ["function", "parameters"],
                },
            },
        },
        "handler": _run_models,
    },
    "run_python": {
        "schema": {
            "type": "function",
            "function": {
                "name": "run_python",
                "description": "Execute arbitrary Python code in a sandboxed environment. Pre-loaded: pull_ticker_data, snapshot, financials, history, analysis, math, statistics. Allowed imports: numpy, pandas, math, statistics, datetime, timezone, yfinance.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Python code to execute.",
                        },
                        "imports": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Extra modules to import.",
                        },
                    },
                    "required": ["code"],
                },
            },
        },
        "handler": _run_python,
    },
    "execute_trade": {
        "schema": {
            "type": "function",
            "function": {
                "name": "execute_trade",
                "description": "Execute a paper trade (BUY or SELL) via the trading engine at live market prices.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["BUY", "SELL"],
                            "description": "Trade action.",
                        },
                        "symbol": {
                            "type": "string",
                            "description": "Ticker symbol.",
                        },
                        "quantity": {
                            "type": "integer",
                            "description": "Number of shares/contracts.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Rationale for the trade.",
                        },
                        "sell_conditions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string"},
                                    "target": {"type": "number"},
                                    "operator": {"type": "string"},
                                    "reason": {"type": "string"},
                                    "metric": {"type": "string"},
                                },
                                "required": ["type", "target", "operator", "reason"],
                            },
                            "description": "Sell conditions for BUY orders.",
                        },
                        "force": {
                            "type": "boolean",
                            "description": "Skip market-hours check (for testing).",
                        },
                    },
                    "required": ["action", "symbol", "quantity", "reason"],
                },
            },
        },
        "handler": _execute_trade,
    },
    "get_portfolio": {
        "schema": {
            "type": "function",
            "function": {
                "name": "get_portfolio",
                "description": "Get the current portfolio state with live valuations, positions, P&L, and sell condition status.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        "handler": _get_portfolio,
    },
    "check_sell_conditions": {
        "schema": {
            "type": "function",
            "function": {
                "name": "check_sell_conditions",
                "description": "Evaluate all sell conditions on all open positions. Returns triggered and approaching conditions.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
        "handler": _check_sell_conditions,
    },

}


def get_tools_for_role(role: str) -> list[dict]:
    """Return the OpenAI-format tool schemas available to a given agent role."""
    role_tools_map = {
        "economist": [],
        "researcher": [],
        "analyst": [
            "pull_market_data", "run_model", "run_python"
        ],
        "associates": [],
        "manager": [
            "execute_trade", "get_portfolio", "check_sell_conditions"
        ],
    }
    tool_names = role_tools_map.get(role, [])
    return [TOOL_REGISTRY[name]["schema"] for name in tool_names if name in TOOL_REGISTRY]


def execute_tool_call(tool_name: str, arguments: dict) -> Any:
    """Execute a tool call by name with the given arguments."""
    entry = TOOL_REGISTRY.get(tool_name)
    if entry is None:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        result = entry["handler"](arguments)
        return result
    except Exception as e:
        log.error("Tool '%s' failed: %s", tool_name, e, exc_info=True)
        return {"error": f"{type(e).__name__}: {e}"}
