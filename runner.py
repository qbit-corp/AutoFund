#!/usr/bin/env python3
"""
AutoFund — Daily Pipeline Runner
================================
Designed to be called via cron job once per day.
Checks if today is a US market trading day (NYSE).
If yes, runs the full daily research pipeline.
If no, exits gracefully.
"""

import logging
import sys
from pathlib import Path

import pandas as pd
import pytz

# Add project root to path so we can import internal modules
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline import run_pipeline
from trader.market_hours import _get_calendar

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("runner")


def is_us_trading_day() -> bool:
    """Check if today is a valid trading day for the NYSE."""
    cal = _get_calendar("NYSE")
    tz = pytz.timezone(str(cal.tz))
    now = pd.Timestamp.now(tz=tz)
    
    try:
        schedule = cal.schedule(
            start_date=now.normalize(),
            end_date=now.normalize(),
        )
        return not schedule.empty
    except Exception as exc:
        log.warning("Could not fetch schedule for NYSE: %s. Assuming market is closed.", exc)
        return False


def main():
    is_cron = "--cron" in sys.argv

    log.info("Checking if today is a US market trading day...")
    if not is_us_trading_day():
        log.info("Today is a weekend or market holiday. Pipeline will not run.")
        sys.exit(0)
        
    log.info("US Market is open today. Initiating the daily research pipeline.")
    try:
        run_pipeline()
        log.info("Pipeline completed successfully.")
        
        # Send success notification
        if is_cron:
            try:
                from telegram_notifier import send_telegram_message, format_portfolio_html
                from trader.engine import PaperTrader
                pt = PaperTrader()
                portfolio = pt.get_portfolio()
                
                today_iso = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
                txns = pt.get_transaction_history(limit=50)
                today_txns = [t for t in txns if t.get("timestamp", "").startswith(today_iso)]
                
                if today_txns:
                    actions = "\n".join([f"• {t['type']} {t['shares']}x {t['symbol']} @ ${t['price']:,.2f}" for t in today_txns])
                    decision_summary = f"<b>Executed Trades Today:</b>\n{actions}"
                else:
                    decision_summary = "<b>Decision:</b> Manager decided to HOLD (No trades executed)."
                    
                msg = "✅ <b>AutoFund Pipeline Completed</b>\n\n"
                msg += f"{decision_summary}\n\n"
                msg += format_portfolio_html(portfolio)
                
                send_telegram_message(msg)
            except Exception as notify_exc:
                log.error("Failed to send success notification: %s", notify_exc)
            
    except Exception as exc:
        log.error("Pipeline failed: %s", exc, exc_info=True)
        
        # Send failure notification
        if is_cron:
            try:
                from telegram_notifier import send_telegram_message
                send_telegram_message(f"❌ <b>AutoFund Pipeline Failed</b>\n\nException: {exc}")
            except Exception as notify_exc:
                log.error("Failed to send failure notification: %s", notify_exc)
            
        sys.exit(1)


if __name__ == "__main__":
    main()
