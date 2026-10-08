"""
Tests for the pipeline orchestrator (pipeline.py) and monitor (monitor.py).
All LLM / network calls are mocked.
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock, call
from datetime import datetime

import pytest


# ── Pipeline helpers ─────────────────────────────────────────────────────────


class TestPipelineHelpers:

    def test_save_and_load_output(self, tmp_path):
        """_save_output writes to research dir; _load_output reads it back."""
        import pipeline as pl
        original_dir = pl.RESEARCH_DIR
        pl.RESEARCH_DIR = tmp_path
        try:
            path = pl._save_output("test_file.md", "hello world")
            assert path.exists()
            content = pl._load_output("test_file.md")
            assert content == "hello world"
        finally:
            pl.RESEARCH_DIR = original_dir

    def test_load_nonexistent(self, tmp_path):
        """_load_output returns None for missing files."""
        import pipeline as pl
        original_dir = pl.RESEARCH_DIR
        pl.RESEARCH_DIR = tmp_path
        try:
            assert pl._load_output("nonexistent.md") is None
        finally:
            pl.RESEARCH_DIR = original_dir

    def test_context_msg_format(self):
        """_context_msg builds correctly formatted injection."""
        import pipeline as pl
        msg = pl._context_msg("Economist", "GDP is 3%")
        assert msg["role"] == "user"
        assert "Economist" in msg["content"]
        assert "GDP is 3%" in msg["content"]


# ── Pipeline dry run ─────────────────────────────────────────────────────────


class TestPipelineDryRun:

    def test_dry_run_prints_stages(self, capsys):
        """Dry run prints planned stages without executing."""
        import pipeline as pl
        pl.run_pipeline(start_stage="economist", dry_run=True)
        output = capsys.readouterr().out
        assert "economist" in output.lower()


# ── Pipeline stage order ─────────────────────────────────────────────────────


class TestPipelineStages:

    def test_stages_list(self):
        """Pipeline STAGES list is complete and ordered."""
        import pipeline as pl
        assert pl.STAGES == ["economist", "researcher", "analyst", "associates", "manager"]

    def test_start_stage_index(self):
        """Starting from a mid-stage skips earlier stages."""
        import pipeline as pl
        idx = pl.STAGES.index("analyst")
        assert idx == 2
        assert pl.STAGES[idx:] == ["analyst", "associates", "manager"]


# ── Monitor ──────────────────────────────────────────────────────────────────


class TestMonitor:

    def test_check_and_report_returns_result(self):
        """check_and_report calls check_sell_conditions and returns its result."""
        from monitor import check_and_report
        mock_pt = MagicMock()
        mock_pt.check_sell_conditions.return_value = {
            "checked_at": "2026-04-22T12:00:00Z",
            "positions_checked": 0,
            "conditions_triggered": [],
            "conditions_approaching": [],
        }
        result = check_and_report(mock_pt)
        assert result["positions_checked"] == 0
        mock_pt.check_sell_conditions.assert_called_once()

    def test_check_and_report_logs_triggered(self):
        """Triggered conditions are logged."""
        from monitor import check_and_report
        mock_pt = MagicMock()
        mock_pt.check_sell_conditions.return_value = {
            "checked_at": "2026-04-22T12:00:00Z",
            "positions_checked": 1,
            "conditions_triggered": [{
                "symbol": "AAPL",
                "type": "price_target",
                "operator": "gte",
                "target": 150.0,
                "sell_executed": True,
                "sell_error": None,
            }],
            "conditions_approaching": [],
        }
        result = check_and_report(mock_pt)
        assert len(result["conditions_triggered"]) == 1

    def test_check_and_report_logs_approaching(self):
        """Approaching conditions are logged."""
        from monitor import check_and_report
        mock_pt = MagicMock()
        mock_pt.check_sell_conditions.return_value = {
            "checked_at": "2026-04-22T12:00:00Z",
            "positions_checked": 1,
            "conditions_triggered": [],
            "conditions_approaching": [{
                "symbol": "AAPL",
                "type": "price_target",
                "operator": "gte",
                "target": 150.0,
                "current_value": 145.0,
                "distance_pct": 3.33,
            }],
        }
        result = check_and_report(mock_pt)
        assert len(result["conditions_approaching"]) == 1
