"""
AutoFund — Code Interpreter Tool
=================================
Sandboxed Python execution for the Analyst's Layer 2 calculations.
Executes arbitrary user-written Python with access to market data and
standard scientific libraries, without access to the filesystem, network
(beyond yfinance), or subprocess operations.

Usage:
    result = run_python(
        code="print(snapshot['current_price'])",
        imports=["numpy"]
    )
"""

import builtins
import ctypes
import io
import traceback
import threading
from contextlib import redirect_stdout
from datetime import datetime, timezone
from typing import Any

# Allowed extra import modules (beyond builtins and standard math/science)
_ALLOWED_IMPORTS = {
    "numpy", "pandas", "math", "statistics",
    "datetime", "timezone",
    "yfinance",
    # math submodules
    "math", "statistics",
}

# Dangerous names to remove from builtins
_BLOCKED_BUILTINS = {
    "open", "input", "eval", "exec", "compile",
    "breakpoint", "exit", "quit",
    "getattr", "setattr", "delattr", "hasattr",
    "reload", "vars", "dir", "globals", "locals",
    "memoryview", "buffer",
    "callable",
    "classmethod", "staticmethod",
    "property",
    "__loader__", "__spec__", "__package__",
    "__name__", "__qualname__", "__annotations__", "__doc__",
    "__file__", "__path__", "__module__", "__dict__",
}

# Modules explicitly blocked (file/network/subprocess access)
_BLOCKED_MODULES = {
    "os", "subprocess", "socket", "requests", "urllib",
    "http", "ftp", "smtplib", "poplib", "imaplib",
    "codecs", "html", "xml", "pickle", "shelve",
    "sqlite3", "dbm", "gzip", "zipfile", "tarfile",
    "fileinput", "pathlib", "glob", "fnmatch", "linecache",
    "tempfile", "shutil", "logging", "getpass", "platform",
    "fpecture", "fcntl", "select", "poll", "epoll", "kqueue",
    "mmap", "readline",
    "multiprocessing", "concurrent", "asyncio",
    "pty", "tty", "termios",
    "pwd", "grp", "crypt", "spwd",
    "resource", "syslog", "ast", "parser",
    "dis", "inspect", "gc",
}


class _ExecutionTimeout(Exception):
    """Raised when code execution exceeds the time limit."""
    pass


def run_python(code: str, imports: list[str] | None = None) -> dict[str, Any]:
    """
    Execute arbitrary Python code in a sandboxed environment.

    Pre-loaded context available to the executing code:
        pull_ticker_data  — pull raw yfinance data (see analyst/market_data.py)
        snapshot          — last data pull snapshot dict (call pull_ticker_data first)
        financials        — income stmt, balance sheet, cash flow
        history           — OHLCV data + stats
        analysis          — analyst targets, estimates, upgrades

    Parameters
    ----------
    code : str
        Python code to execute.
    imports : list[str], optional
        Extra modules to whitelist beyond the default set
        (numpy, pandas, math, statistics, datetime, timezone, yfinance).

    Returns
    -------
    dict with keys:
        "success": bool
        "result": arbitrary — last expression value (if any)
        "stdout": str — captured stdout during execution
        "error": str or None
        "traceback": str or None — full traceback on error
        "execution_time_ms": float
        "data_source": str — "market_data" if pull_ticker_data was called
    """
    start = datetime.now(timezone.utc)

    # Validate extra imports
    requested_imports = set(imports or [])
    unknown = requested_imports - _ALLOWED_IMPORTS
    if unknown:
        return {
            "success": False,
            "result": None,
            "stdout": "",
            "error": f"Imports not allowed: {sorted(unknown)}. "
                     f"Allowed: {sorted(_ALLOWED_IMPORTS)}",
            "traceback": None,
            "execution_time_ms": (datetime.now(timezone.utc) - start).total_seconds() * 1000,
            "data_source": None,
        }

    # Build safe builtins dict — use a minimal known-safe set of builtins
    _SAFE_BUILTINS = [
        "abs", "all", "any", "ascii", "bin", "bool", "bytes", "chr", "complex",
        "dict", "enumerate", "filter", "float", "format", "frozenset", "hash", "hex",
        "id", "int", "isinstance", "issubclass", "iter", "len", "list", "map",
        "max", "min", "next", "oct", "ord", "pow", "print", "range", "repr", "reversed",
        "round", "set", "slice", "sorted", "str", "sum", "tuple", "type", "zip",
        "True", "False", "None", "NotImplemented", "Ellipsis",
    ]
    safe_builtins = {name: getattr(builtins, name) for name in _SAFE_BUILTINS
                     if hasattr(builtins, name)}

    # Provide a restricted __import__ that only allows whitelisted modules
    # and blocks dangerous ones (os, subprocess, socket, etc.)
    _all_allowed = _ALLOWED_IMPORTS | set(requested_imports or [])

    def _restricted_import(name, *args, **kwargs):
        top_level = name.split(".")[0]
        if top_level in _BLOCKED_MODULES:
            raise ImportError(f"Import of '{name}' is blocked in the sandbox.")
        if top_level not in _all_allowed:
            raise ImportError(
                f"Import of '{name}' is not allowed. "
                f"Allowed: {sorted(_all_allowed)}"
            )
        return __import__(name, *args, **kwargs)

    safe_builtins["__import__"] = _restricted_import

    # Pre-load modules into the execution namespace
    exec_globals: dict[str, Any] = {
        "__builtins__": safe_builtins,
    }

    # Import allowed extra modules
    for mod_name in requested_imports:
        if mod_name in _ALLOWED_IMPORTS:
            try:
                exec_globals[mod_name] = __import__(mod_name)
            except Exception as e:
                return {
                    "success": False,
                    "result": None,
                    "stdout": "",
                    "error": f"Failed to import '{mod_name}': {e}",
                    "traceback": None,
                    "execution_time_ms": (datetime.now(timezone.utc) - start).total_seconds() * 1000,
                    "data_source": None,
                }

    # Pre-load context variables (callable, mutable — shared state across calls within a session)
    # snapshot / financials / history / analysis are placed in the namespace as empty dicts
    # so code can read them after calling pull_ticker_data
    exec_globals["snapshot"] = {}
    exec_globals["financials"] = {}
    exec_globals["history"] = {}
    exec_globals["analysis"] = {}

    # Provide pull_ticker_data that auto-updates the namespace variables
    def _pull_ticker_data(symbol: str, modules: list[str] | None = None,
                          period: str = "1y", interval: str = "1d") -> dict:
        """Fetch yfinance data and populate the shared context vars."""
        from analyst.market_data import pull_ticker_data as _pull
        data = _pull(symbol, modules=modules, period=period, interval=interval)
        # Update shared context
        exec_globals["snapshot"].update(data.get("snapshot", {}))
        exec_globals["financials"].update(data.get("financials", {}))
        exec_globals["history"].update(data.get("history", {}))
        exec_globals["analysis"].update(data.get("analysis", {}))
        return data

    exec_globals["pull_ticker_data"] = _pull_ticker_data

    # Pre-load math and statistics
    exec_globals["math"] = __import__("math")
    exec_globals["statistics"] = __import__("statistics")

    # Capture stdout
    captured = io.StringIO()

    # Timeout mechanism — run exec() in a worker thread and kill it if it
    # exceeds the time limit.  threading.Timer alone cannot interrupt the
    # main thread's exec(); we use ctypes to inject an async exception into
    # the worker thread.
    exec_result: dict[str, Any] = {"success": False, "result": None, "error": None, "tb": None}

    def _exec_worker():
        try:
            with redirect_stdout(captured):
                exec(code, exec_globals)
            exec_result["success"] = True
            exec_result["result"] = exec_globals.get("_result", None)
        except _ExecutionTimeout:
            exec_result["error"] = "Execution timed out after 30 seconds."
        except Exception as e:
            exec_result["error"] = f"{type(e).__name__}: {e}"
            exec_result["tb"] = "".join(traceback.format_exception(type(e), e, e.__traceback__))

    worker = threading.Thread(target=_exec_worker, daemon=True)
    worker.start()
    worker.join(timeout=30.0)

    if worker.is_alive():
        # Inject TimeoutError into the worker thread
        try:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(worker.ident),
                ctypes.py_object(_ExecutionTimeout),
            )
        except Exception:
            pass
        worker.join(timeout=2.0)
        exec_result["error"] = "Execution timed out after 30 seconds."
        exec_result["success"] = False

    success = exec_result["success"]
    result = exec_result["result"]
    error = exec_result["error"]
    tb_str = exec_result["tb"]

    elapsed_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000

    # Determine data source
    data_source = None
    if exec_globals.get("snapshot") and exec_globals["snapshot"]:
        data_source = "market_data"

    return {
        "success": success,
        "result": result,
        "stdout": captured.getvalue(),
        "error": error,
        "traceback": tb_str,
        "execution_time_ms": round(elapsed_ms, 2),
        "data_source": data_source,
    }
