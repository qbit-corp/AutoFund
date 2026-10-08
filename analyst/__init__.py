"""
AutoFund Analyst Package
=======================
Exports market data tools and financial model functions.
"""

from analyst.market_data import (
    pull_ticker_data,
    get_snapshot,
    get_history,
    get_financials,
    get_analysis,
    get_technicals,
    get_holders,
    ALL_MODULES,
)

from analyst.models import (
    dcf_valuation,
    projected_income_statement,
    technical_summary,
    peer_valuation_multiples,
    scenario_model,
    capm,
    dividend_discount_model,
    compound_growth_rate,
)

from analyst.interpreter import run_python

__all__ = [
    # Market data
    "pull_ticker_data",
    "get_snapshot",
    "get_history",
    "get_financials",
    "get_analysis",
    "get_technicals",
    "get_holders",
    "ALL_MODULES",
    # Financial models
    "dcf_valuation",
    "projected_income_statement",
    "technical_summary",
    "peer_valuation_multiples",
    "scenario_model",
    "capm",
    "dividend_discount_model",
    "compound_growth_rate",
    # Code interpreter
    "run_python",
]