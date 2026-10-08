"""
AutoFund — Ticker Validation & Exchange Classification
========================================================
Defines the tradeable universe for paper trading:
  ✓  US stocks (NYSE, NASDAQ)
  ✓  European stocks (LSE, Xetra, Euronext, etc.)
  ✓  Commodities (Gold, Oil, Gas, Silver, etc.)
  ✗  Asian equities
  ✗  Options / derivatives
  ✗  Cryptocurrencies
  ✗  Private equity / OTC
"""

import logging

import yfinance as yf

log = logging.getLogger("trader.assets")

# ── Explicitly allowed commodity futures ──────────────────────────────────────
# Calendar names must match pandas_market_calendars registered classes.
# The library uses specific Globex product calendars, e.g. 'CMEGlobex_GC'.
ALLOWED_COMMODITIES: dict[str, dict] = {
    "GC=F":  {"name": "Gold Futures",              "calendar": "CMEGlobex_GC"},
    "SI=F":  {"name": "Silver Futures",             "calendar": "CMEGlobex_SI"},
    "CL=F":  {"name": "WTI Crude Oil Futures",      "calendar": "CMEGlobex_CL"},
    "BZ=F":  {"name": "Brent Crude Oil Futures",     "calendar": "CMEGlobex_CL"},  # Brent uses similar hours
    "NG=F":  {"name": "Natural Gas Futures",         "calendar": "CMEGlobex_NG"},
    "HG=F":  {"name": "Copper Futures",              "calendar": "CMEGlobex_HG"},
    "PL=F":  {"name": "Platinum Futures",            "calendar": "CMEGlobex_PL"},
}

# ── US exchange codes as returned by yfinance .info["exchange"] ───────────────
US_EXCHANGES: set[str] = {
    "NMS",   # NASDAQ Global Select Market
    "NYQ",   # NYSE
    "NGM",   # NASDAQ Global Market
    "NCM",   # NASDAQ Capital Market
    "PCX",   # NYSE Arca
    "BTS",   # CBOE BZX Exchange
    "ASE",   # NYSE American (formerly AMEX)
    "NYE",   # NYSE alternate code
}

# ── European exchange → pandas_market_calendars calendar mapping ──────────────
# Calendar names must match registered classes.  Use MIC-style codes (XAMS,
# XPAR, XLON, etc.) which pandas_market_calendars recognises.
EU_EXCHANGE_CALENDAR: dict[str, str] = {
    "LSE": "LSE",           # London Stock Exchange
    "GER": "XETR",          # Frankfurt / Xetra
    "FRA": "XFRA",          # Frankfurt alternate code
    "AMS": "XAMS",          # Euronext Amsterdam
    "PAR": "XPAR",          # Euronext Paris
    "BRU": "XBRU",          # Euronext Brussels
    "LIS": "XLIS",          # Euronext Lisbon
    "MIL": "XMIL",          # Borsa Italiana
    "MCE": "XMAD",          # Bolsa de Madrid
    "STO": "XSTO",          # Stockholm (Nasdaq Nordic)
    "HEL": "XHEL",          # Helsinki (Nasdaq Nordic)
    "CPH": "XCSE",          # Copenhagen (Nasdaq Nordic)
    "OSL": "XOSL",          # Oslo Bors
    "VIE": "XWBO",          # Vienna Stock Exchange
    "EBS": "SIX",           # SIX Swiss Exchange
    "ISE": "XDUB",          # Irish Stock Exchange (Euronext Dublin)
}

# ── Asian exchanges — explicitly blocked ──────────────────────────────────────
ASIAN_EXCHANGES: set[str] = {
    "JPX", "TYO",           # Tokyo
    "HKG",                  # Hong Kong
    "SHH", "SHZ",           # Shanghai / Shenzhen
    "KSC", "KOE",           # Korea (KOSPI / KOSDAQ)
    "TAI",                  # Taiwan
    "BOM", "NSI", "NSE",    # India (BSE / NSE)
    "SGX",                  # Singapore
    "SET",                  # Thailand
    "JKT",                  # Jakarta
    "KLS",                  # Kuala Lumpur
}

# ── Blocked quote types ───────────────────────────────────────────────────────
BLOCKED_QUOTE_TYPES: set[str] = {
    "OPTION",
    "CRYPTOCURRENCY",
    "MUTUALFUND",
}

# ── In-memory validation cache ────────────────────────────────────────────────
# Maps symbol → (is_valid, message, calendar_name | None)
_validation_cache: dict[str, tuple[bool, str, str | None]] = {}


def validate_ticker(symbol: str) -> tuple[bool, str]:
    """
    Check whether a ticker is allowed for paper trading.

    Returns
    -------
    (is_valid, message)
        *is_valid* is ``True`` when the ticker may be traded.
        *message* explains why the ticker was accepted or rejected.
    """
    symbol = symbol.upper().strip()

    # ── Fast path: commodity allow-list (no network call) ─────────────────
    if symbol in ALLOWED_COMMODITIES:
        return True, f"Allowed commodity: {ALLOWED_COMMODITIES[symbol]['name']}"

    # ── Check cache ───────────────────────────────────────────────────────
    if symbol in _validation_cache:
        valid, msg, _ = _validation_cache[symbol]
        return valid, msg

    # ── Fetch ticker metadata from yfinance ───────────────────────────────
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
    except Exception as exc:
        result = (False, f"Failed to fetch ticker info: {exc}", None)
        _validation_cache[symbol] = result
        return result[0], result[1]

    quote_type = info.get("quoteType", "")
    exchange = info.get("exchange", "")

    # Ticker doesn't exist
    if not quote_type and not exchange:
        result = (False, "Ticker not found or invalid", None)
        _validation_cache[symbol] = result
        return result[0], result[1]

    # Block options, crypto, mutual funds
    if quote_type in BLOCKED_QUOTE_TYPES:
        result = (
            False,
            f"Asset type '{quote_type}' is not allowed "
            "(no options, crypto, or mutual funds)",
            None,
        )
        _validation_cache[symbol] = result
        return result[0], result[1]

    # ── US exchange ───────────────────────────────────────────────────────
    if exchange in US_EXCHANGES:
        name = info.get("longName") or info.get("shortName") or symbol
        result = (True, f"US equity on {exchange}: {name}", "NYSE")
        _validation_cache[symbol] = result
        return result[0], result[1]

    # ── European exchange ─────────────────────────────────────────────────
    if exchange in EU_EXCHANGE_CALENDAR:
        calendar = EU_EXCHANGE_CALENDAR[exchange]
        name = info.get("longName") or info.get("shortName") or symbol
        result = (True, f"European equity on {exchange}: {name}", calendar)
        _validation_cache[symbol] = result
        return result[0], result[1]

    # ── Asian exchange (explicit block) ───────────────────────────────────
    if exchange in ASIAN_EXCHANGES:
        result = (
            False,
            f"Asian exchange '{exchange}' is not allowed. "
            "Only US and European markets are supported.",
            None,
        )
        _validation_cache[symbol] = result
        return result[0], result[1]

    # ── Unknown exchange ──────────────────────────────────────────────────
    result = (
        False,
        f"Exchange '{exchange}' (quoteType='{quote_type}') "
        "is not in the approved list. Only US/EU stocks and "
        "commodity futures are supported.",
        None,
    )
    _validation_cache[symbol] = result
    return result[0], result[1]


def get_exchange_calendar(symbol: str) -> str:
    """
    Return the ``pandas_market_calendars`` calendar name for *symbol*.

    The ticker should already have been validated via :func:`validate_ticker`;
    if it hasn't, this function will validate it as a side effect.
    """
    symbol = symbol.upper().strip()

    # Commodities
    if symbol in ALLOWED_COMMODITIES:
        return ALLOWED_COMMODITIES[symbol]["calendar"]

    # Cached result
    if symbol in _validation_cache:
        _, _, calendar = _validation_cache[symbol]
        return calendar or "NYSE"

    # Not yet validated — trigger validation (populates cache)
    validate_ticker(symbol)
    if symbol in _validation_cache:
        _, _, calendar = _validation_cache[symbol]
        return calendar or "NYSE"

    return "NYSE"  # ultimate fallback


def clear_cache() -> None:
    """Clear the validation cache (useful for testing)."""
    _validation_cache.clear()
