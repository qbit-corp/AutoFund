"""
Tests for trader/allowed_assets.py — ticker validation and exchange classification.
All yfinance calls are mocked.
"""

from unittest.mock import patch, MagicMock

import pytest

from trader.allowed_assets import (
    validate_ticker,
    get_exchange_calendar,
    clear_cache,
    ALLOWED_COMMODITIES,
    US_EXCHANGES,
    EU_EXCHANGE_CALENDAR,
    ASIAN_EXCHANGES,
    BLOCKED_QUOTE_TYPES,
)


@pytest.fixture(autouse=True)
def clean_cache():
    """Clear the validation cache before each test."""
    clear_cache()
    yield
    clear_cache()


# ── Commodity allow-list ─────────────────────────────────────────────────────


class TestCommodityValidation:

    @pytest.mark.parametrize("symbol", ["GC=F", "SI=F", "CL=F", "BZ=F", "NG=F", "HG=F", "PL=F"])
    def test_commodity_allowed(self, symbol):
        """All whitelisted commodities pass without network calls."""
        valid, msg = validate_ticker(symbol)
        assert valid is True
        assert "commodity" in msg.lower()

    def test_commodity_calendar(self):
        """Commodities map to correct CME calendar."""
        assert get_exchange_calendar("GC=F") == "CMEGlobex_GC"
        assert get_exchange_calendar("CL=F") == "CMEGlobex_CL"


# ── US exchange validation ───────────────────────────────────────────────────


class TestUSExchange:

    def test_us_stock_accepted(self):
        """A ticker on a US exchange is accepted."""
        mock_info = {"quoteType": "EQUITY", "exchange": "NMS", "longName": "Apple Inc."}
        with patch("trader.allowed_assets.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = mock_info
            valid, msg = validate_ticker("AAPL")
        assert valid is True
        assert "US equity" in msg

    @pytest.mark.parametrize("exchange", ["NMS", "NYQ", "NGM", "NCM", "PCX", "BTS", "ASE"])
    def test_all_us_exchanges_accepted(self, exchange):
        """All US exchange codes are accepted."""
        mock_info = {"quoteType": "EQUITY", "exchange": exchange, "shortName": "Test"}
        with patch("trader.allowed_assets.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = mock_info
            valid, _ = validate_ticker(f"TEST_{exchange}")
        assert valid is True


# ── European exchange validation ─────────────────────────────────────────────


class TestEuropeanExchange:

    def test_eu_stock_accepted(self):
        """A ticker on a European exchange is accepted."""
        mock_info = {"quoteType": "EQUITY", "exchange": "LSE", "longName": "BP plc"}
        with patch("trader.allowed_assets.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = mock_info
            valid, msg = validate_ticker("BP.L")
        assert valid is True
        assert "European" in msg

    def test_eu_calendar_mapping(self):
        """European exchanges map to correct calendars."""
        mock_info = {"quoteType": "EQUITY", "exchange": "GER", "shortName": "SAP"}
        with patch("trader.allowed_assets.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = mock_info
            validate_ticker("SAP.DE")
        cal = get_exchange_calendar("SAP.DE")
        assert cal == "XETR"


# ── Blocked assets ───────────────────────────────────────────────────────────


class TestBlockedAssets:

    def test_asian_exchange_blocked(self):
        """Asian exchanges are rejected."""
        mock_info = {"quoteType": "EQUITY", "exchange": "JPX", "longName": "Toyota"}
        with patch("trader.allowed_assets.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = mock_info
            valid, msg = validate_ticker("7203.T")
        assert valid is False
        assert "Asian" in msg

    def test_crypto_blocked(self):
        """Crypto is rejected."""
        mock_info = {"quoteType": "CRYPTOCURRENCY", "exchange": "CCC"}
        with patch("trader.allowed_assets.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = mock_info
            valid, msg = validate_ticker("BTC-USD")
        assert valid is False
        assert "CRYPTOCURRENCY" in msg

    def test_option_blocked(self):
        """Options are rejected."""
        mock_info = {"quoteType": "OPTION", "exchange": "OPR"}
        with patch("trader.allowed_assets.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = mock_info
            valid, msg = validate_ticker("AAPL240517C00200000")
        assert valid is False

    def test_mutual_fund_blocked(self):
        """Mutual funds are rejected."""
        mock_info = {"quoteType": "MUTUALFUND", "exchange": "NAS"}
        with patch("trader.allowed_assets.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = mock_info
            valid, msg = validate_ticker("VFIAX")
        assert valid is False

    def test_unknown_exchange_blocked(self):
        """Unrecognized exchange is rejected."""
        mock_info = {"quoteType": "EQUITY", "exchange": "ZZZZZ"}
        with patch("trader.allowed_assets.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = mock_info
            valid, msg = validate_ticker("WEIRD")
        assert valid is False
        assert "not in the approved list" in msg


# ── Edge cases ───────────────────────────────────────────────────────────────


class TestValidationEdgeCases:

    def test_nonexistent_ticker(self):
        """A ticker with no data is rejected."""
        mock_info = {}
        with patch("trader.allowed_assets.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = mock_info
            valid, msg = validate_ticker("ZZZZZ123")
        assert valid is False
        assert "not found" in msg.lower()

    def test_yfinance_exception(self):
        """Exception from yfinance is handled gracefully."""
        with patch("trader.allowed_assets.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = None
            MockTicker.side_effect = Exception("network error")
            valid, msg = validate_ticker("FAIL")
        assert valid is False
        assert "network error" in msg.lower()

    def test_cache_hit(self):
        """Second validation uses cache — no second yfinance call."""
        mock_info = {"quoteType": "EQUITY", "exchange": "NMS", "longName": "Test"}
        with patch("trader.allowed_assets.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = mock_info
            validate_ticker("CACHE_TEST")
            validate_ticker("CACHE_TEST")
        # yf.Ticker should only be called once
        assert MockTicker.call_count == 1

    def test_symbol_normalised(self):
        """Symbols are uppercased and stripped."""
        mock_info = {"quoteType": "EQUITY", "exchange": "NMS", "longName": "Test"}
        with patch("trader.allowed_assets.yf.Ticker") as MockTicker:
            MockTicker.return_value.info = mock_info
            valid, _ = validate_ticker("  aapl  ")
        assert valid is True

    def test_clear_cache(self):
        """clear_cache empties the validation cache."""
        from trader.allowed_assets import _validation_cache
        _validation_cache["TEST"] = (True, "test", "NYSE")
        assert len(_validation_cache) > 0
        clear_cache()
        assert len(_validation_cache) == 0
