"""
AutoFund Paper Trading Engine
==============================
Provides a programmatic API for AI agents to execute paper trades
against real market data via yfinance, restricted to US/European
stocks and commodities, and only during market hours.

Usage (agent):
    from trader import PaperTrader

    pt = PaperTrader()
    pt.buy("AAPL", 10, reason="Strong fundamentals per Analyst report")
    pt.sell("GC=F", 2, reason="Rebalancing commodity exposure")
    portfolio = pt.get_portfolio()

Usage (CLI):
    python -m trader portfolio
    python -m trader buy AAPL 10
    python -m trader status
"""

from .engine import PaperTrader, TradeError

__all__ = ["PaperTrader", "TradeError"]
