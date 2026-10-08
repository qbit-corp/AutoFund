"""
AutoFund — Continuous Monitoring Loop (Loop B)
================================================
Evaluates sell conditions on all open positions every N minutes.
Designed to run as a background process or cron job.

When a condition is triggered:
  - If the market is open → execute the sell immediately
  - If the market is closed → queue in pending_sells, execute at next open

Usage:
    python monitor.py                   # Run once (for cron)
    python monitor.py --cron            # NYSE hours only, with Telegram report
    python monitor.py --loop            # Run continuously (every 15 min)
    python monitor.py --loop --interval 5   # Custom interval (minutes)
    python monitor.py --verbose         # Extra logging
"""

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from trader import PaperTrader
from trader.market_hours import _get_calendar
from telegram_notifier import send_telegram_message, format_portfolio_html

log = logging.getLogger("monitor")


def check_and_report(pt: PaperTrader | None = None) -> dict:
    """
    Run a single monitoring tick.

    Returns the check_sell_conditions() result dict.
    """
    if pt is None:
        pt = PaperTrader()
    log.info("─── Monitor tick at %s ───", datetime.now(timezone.utc).isoformat())

    result = pt.check_sell_conditions()

    # Log summary
    triggered = result.get("conditions_triggered", [])
    approaching = result.get("conditions_approaching", [])
    positions_checked = result.get("positions_checked", 0)

    log.info(
        "Checked %d positions  |  %d triggered  |  %d approaching",
        positions_checked, len(triggered), len(approaching),
    )

    for t in triggered:
        executed = "✓ SOLD" if t.get("sell_executed") else f"⏳ {t.get('sell_error', 'queued')}"
        log.warning(
            "  TRIGGERED: %s  %s %s %s  →  %s",
            t["symbol"], t["type"], t["operator"], t["target"], executed,
        )

    for a in approaching:
        log.info(
            "  APPROACHING: %s  %s %s %s  (current: %s, distance: %.1f%%)",
            a.get("symbol", "?"), a.get("type", "?"), a.get("operator", "?"),
            a.get("target", "?"),
            a.get("current_value", "?"), float(a.get("distance_pct") or 0),
        )

    if not triggered and not approaching:
        log.info("  All conditions distant. Portfolio healthy.")

    return result


def is_us_market_open(now: pd.Timestamp | None = None) -> bool:
    """Use the NYSE session calendar, including holidays and early closes."""
    cal = _get_calendar("NYSE")
    now = pd.Timestamp.now(tz=cal.tz) if now is None else now.tz_convert(cal.tz)
    schedule = cal.schedule(start_date=now.date(), end_date=now.date())
    if schedule.empty:
        return False
    # Check the range first: open_at_time raises outside the session range.
    session = schedule.iloc[0]
    if not session["market_open"] <= now < session["market_close"]:
        return False
    return bool(cal.open_at_time(schedule, now))


def run_cron_check() -> int:
    """Run one market-hours check and send its report to Telegram."""
    try:
        if not is_us_market_open():
            log.info("NYSE is closed. Skipping the scheduled monitor check.")
            return 0

        pt = PaperTrader()
        result = check_and_report(pt)
        triggered = result.get("conditions_triggered", [])
        approaching = result.get("conditions_approaching", [])
        lines = [
            "📊 <b>AutoFund Hourly Monitor</b>",
            f"Positions checked: {result.get('positions_checked', 0)}",
            f"Conditions triggered: {len(triggered)}",
            f"Conditions approaching: {len(approaching)}",
        ]
        for condition in triggered:
            status = "SOLD" if condition.get("sell_executed") else "Queued / see log"
            lines.append(
                f"• {escape(str(condition['symbol']))}: {status} "
                f"({escape(str(condition['type']))})"
            )
        lines.extend(["", format_portfolio_html(pt.get_portfolio())])
        print(json.dumps(result, indent=2, default=str))
        if not send_telegram_message("\n".join(lines)):
            log.error("Monitor completed, but Telegram delivery failed.")
            return 1
        return 0
    except Exception as exc:
        log.exception("Scheduled monitor failed.")
        send_telegram_message(
            f"❌ <b>AutoFund Monitor Failed</b>\n\n{escape(str(exc))}"
        )
        return 1


def run_loop(interval_minutes: int = 15) -> None:
    """Run the monitoring loop continuously."""
    log.info("═══ Starting continuous monitoring loop (every %d min) ═══", interval_minutes)
    log.info("Press Ctrl+C to stop.")

    pt = PaperTrader()

    while True:
        try:
            check_and_report(pt)
        except KeyboardInterrupt:
            log.info("Monitor stopped by user.")
            raise
        except Exception as e:
            log.error("Monitor tick failed: %s", e, exc_info=True)

        time.sleep(interval_minutes * 60)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="monitor",
        description="AutoFund — Continuous Sell Condition Monitor",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously instead of a single check",
    )
    mode.add_argument(
        "--cron",
        action="store_true",
        help="Run once only during NYSE trading hours and send a Telegram report",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=15,
        help="Minutes between checks when running in loop mode (default: 15)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.cron:
        raise SystemExit(run_cron_check())
    elif args.loop:
        run_loop(interval_minutes=args.interval)
    else:
        result = check_and_report()
        # Print JSON for programmatic consumption (e.g., cron job piped to log)
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
