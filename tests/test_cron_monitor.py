"""Scheduled monitoring must respect NYSE sessions and report its outcome."""

from unittest.mock import MagicMock, patch
import unittest

import pandas as pd

from monitor import is_us_market_open, run_cron_check


class TestCronMonitor(unittest.TestCase):
    def test_nyse_sessions(self):
        cases = [
            ("2026-10-08 15:29", False),
            ("2026-10-08 15:30", True),
            ("2026-10-08 21:30", True),
            ("2026-10-08 22:00", False),
            ("2026-10-10 16:30", False),  # Saturday
            ("2026-11-26 16:30", False),  # Thanksgiving
            ("2026-11-27 18:30", True),  # Black Friday early close at 19:00 Rome
            ("2026-11-27 19:00", False),
            ("2026-10-26 14:29", False),  # Europe changed clocks, US has not
            ("2026-10-26 14:30", True),
            ("2026-10-26 20:30", True),
            ("2026-10-26 21:00", False),
        ]
        for rome_time, expected in cases:
            with self.subTest(rome_time=rome_time):
                self.assertIs(
                    is_us_market_open(pd.Timestamp(rome_time, tz="Europe/Rome")),
                    expected,
                )

    def test_closed_session_does_not_touch_portfolio_or_notify(self):
        with patch("monitor.is_us_market_open", return_value=False), \
             patch("monitor.PaperTrader") as trader, \
             patch("monitor.send_telegram_message") as notify:
            self.assertEqual(run_cron_check(), 0)
            trader.assert_not_called()
            notify.assert_not_called()

    def test_active_check_reports_even_without_sell(self):
        for delivered, exit_code in [(True, 0), (False, 1)]:
            with self.subTest(delivered=delivered):
                trader = MagicMock()
                trader.check_sell_conditions.return_value = {
                    "positions_checked": 2,
                    "conditions_triggered": [],
                    "conditions_approaching": [],
                }
                trader.get_portfolio.return_value = {}
                with patch("monitor.is_us_market_open", return_value=True), \
                     patch("monitor.PaperTrader", return_value=trader), \
                     patch("monitor.send_telegram_message", return_value=delivered) as notify:
                    self.assertEqual(run_cron_check(), exit_code)
                    trader.check_sell_conditions.assert_called_once()
                    message = notify.call_args.args[0]
                    self.assertIn("Positions checked: 2", message)
                    self.assertIn("Account Summary", message)

    def test_monitor_error_alert_is_html_safe(self):
        with patch("monitor.is_us_market_open", return_value=True), \
             patch("monitor.PaperTrader", side_effect=RuntimeError("bad <data>")), \
             patch("monitor.send_telegram_message", return_value=True) as notify:
            self.assertEqual(run_cron_check(), 1)
            self.assertIn("bad &lt;data&gt;", notify.call_args.args[0])

    def test_calendar_failure_does_not_execute_monitor(self):
        with patch("monitor.is_us_market_open", side_effect=RuntimeError("calendar failed")), \
             patch("monitor.PaperTrader") as trader, \
             patch("monitor.send_telegram_message", return_value=True) as notify:
            self.assertEqual(run_cron_check(), 1)
            trader.assert_not_called()
            self.assertIn("calendar failed", notify.call_args.args[0])
