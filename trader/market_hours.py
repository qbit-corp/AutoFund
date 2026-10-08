"""
AutoFund — Market Hours Checker
================================
Uses ``pandas_market_calendars`` to determine whether the relevant
exchange for a given ticker is currently open for trading.

Each ticker is mapped to a calendar via the exchange classification
in :mod:`trader.allowed_assets`.
"""

import logging
from datetime import datetime

import pandas as pd
import pandas_market_calendars as mcal
import pytz

from .allowed_assets import get_exchange_calendar

log = logging.getLogger("trader.hours")

# ── Calendar cache ────────────────────────────────────────────────────────────
_calendar_cache: dict = {}


def _get_calendar(name: str):
    """Return a cached ``pandas_market_calendars`` calendar object."""
    if name not in _calendar_cache:
        try:
            _calendar_cache[name] = mcal.get_calendar(name)
        except Exception as exc:
            log.warning(
                "Calendar '%s' not available (%s). Falling back to NYSE.",
                name,
                exc,
            )
            _calendar_cache[name] = mcal.get_calendar("NYSE")
    return _calendar_cache[name]


# ── Public API ────────────────────────────────────────────────────────────────

def is_market_open(symbol: str) -> tuple[bool, str]:
    """
    Check whether the market for *symbol* is currently open.

    Returns
    -------
    (is_open, message)
        *is_open* is ``True`` if trading is allowed right now.
        *message* is a human-readable status string.
    """
    calendar_name = get_exchange_calendar(symbol)
    cal = _get_calendar(calendar_name)

    tz = pytz.timezone(str(cal.tz))
    now = pd.Timestamp.now(tz=tz)

    # ── Today's schedule ──────────────────────────────────────────────────
    try:
        schedule = cal.schedule(
            start_date=now.normalize(),
            end_date=now.normalize(),
        )
    except Exception as exc:
        return False, f"Could not fetch schedule for {calendar_name}: {exc}"

    # Market closed today (weekend / holiday)
    if schedule.empty:
        next_open_str = _next_open_str(cal, now, tz)
        return False, (
            f"{calendar_name} is closed today (weekend/holiday). "
            f"{next_open_str}"
        )

    # ── Check if we are within trading hours ──────────────────────────────
    market_open = schedule.iloc[0]["market_open"]
    market_close = schedule.iloc[0]["market_close"]

    # Ensure timezone-aware comparison
    if market_open.tzinfo is None:
        market_open = market_open.tz_localize("UTC")
    if market_close.tzinfo is None:
        market_close = market_close.tz_localize("UTC")

    market_open_local = market_open.astimezone(tz)
    market_close_local = market_close.astimezone(tz)

    try:
        is_open = cal.open_at_time(schedule, now)
    except Exception:
        # Fallback: manual range check
        is_open = market_open_local <= now <= market_close_local

    if is_open:
        return True, (
            f"{calendar_name} is OPEN. "
            f"Closes at {market_close_local.strftime('%H:%M %Z')}"
        )

    # Before open
    if now < market_open_local:
        return False, (
            f"{calendar_name} has not opened yet. "
            f"Opens at {market_open_local.strftime('%H:%M %Z')}"
        )

    # After close
    next_open_str = _next_open_str(cal, now, tz)
    return False, (
        f"{calendar_name} is closed for today "
        f"(was {market_open_local.strftime('%H:%M')}"
        f"–{market_close_local.strftime('%H:%M %Z')}). "
        f"{next_open_str}"
    )


def get_all_market_status() -> dict[str, tuple[bool, str]]:
    """
    Return the open / closed status for every relevant exchange at once.

    Returns a dict: ``{exchange_name: (is_open, message)}``.
    """
    exchanges = {
        "NYSE":                       "US Equities",
        "LSE":                        "London",
        "XETR":                       "Frankfurt / Xetra",
        "XAMS":                       "Amsterdam / Paris",
        "CMEGlobex_EnergyAndMetals":  "Commodities (CME)",
    }

    results: dict[str, tuple[bool, str]] = {}

    for cal_name, label in exchanges.items():
        cal = _get_calendar(cal_name)
        tz = pytz.timezone(str(cal.tz))
        now = pd.Timestamp.now(tz=tz)

        try:
            schedule = cal.schedule(
                start_date=now.normalize(),
                end_date=now.normalize(),
            )
        except Exception:
            results[label] = (False, "Schedule unavailable")
            continue

        if schedule.empty:
            results[label] = (False, "Closed (weekend/holiday)")
            continue

        market_open = schedule.iloc[0]["market_open"]
        market_close = schedule.iloc[0]["market_close"]

        if market_open.tzinfo is None:
            market_open = market_open.tz_localize("UTC")
        if market_close.tzinfo is None:
            market_close = market_close.tz_localize("UTC")

        market_open_local = market_open.astimezone(tz)
        market_close_local = market_close.astimezone(tz)

        try:
            is_open = cal.open_at_time(schedule, now)
        except Exception:
            is_open = market_open_local <= now <= market_close_local

        if is_open:
            results[label] = (
                True,
                f"Open until {market_close_local.strftime('%H:%M %Z')}",
            )
        elif now < market_open_local:
            results[label] = (
                False,
                f"Opens at {market_open_local.strftime('%H:%M %Z')}",
            )
        else:
            results[label] = (
                False,
                f"Closed (was {market_open_local.strftime('%H:%M')}"
                f"–{market_close_local.strftime('%H:%M %Z')})",
            )

    return results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _next_open_str(cal, now, tz) -> str:
    """Build a human-readable 'Next open: …' string."""
    try:
        future = cal.schedule(
            start_date=(now + pd.Timedelta(days=1)).normalize(),
            end_date=(now + pd.Timedelta(days=10)).normalize(),
        )
        if not future.empty:
            nxt = future.iloc[0]["market_open"]
            if nxt.tzinfo is None:
                nxt = nxt.tz_localize("UTC")
            nxt_local = nxt.astimezone(tz)
            return f"Next open: {nxt_local.strftime('%a %b %d, %H:%M %Z')}"
    except Exception:
        pass
    return ""
