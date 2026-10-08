"""
AutoFund — Daily Research Pipeline Orchestrator
=================================================
Runs the six-stage investment research pipeline:

  Stage 0 — Economist   (macro briefing)
  Stage 1 — Researcher  (news scan)
  Stage 2 — Analyst     (financial analysis)
  Stage 3 — Associates  (3 independent scenarios)
  Stage 4 — Manager     (final decision + trade execution)

Usage:
    python pipeline.py                      # Run full pipeline
    python pipeline.py --stage economist    # Run only one stage
    python pipeline.py --stage analyst      # Run from analyst onward
    python pipeline.py --dry-run            # Print what would run
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# Load .env file from project root before any client initialization
load_dotenv(Path(__file__).resolve().parent / ".env")

from agents.client import LLMClient
from agents.runner import AgentRunner
from agents.tools import _run_scraper

log = logging.getLogger("pipeline")

PROJECT_ROOT = Path(__file__).resolve().parent
RESEARCH_DIR = PROJECT_ROOT / "research"

# Ensure research directory exists
RESEARCH_DIR.mkdir(exist_ok=True)

TODAY = datetime.now().strftime("%Y-%m-%d")


def _save_output(filename: str, content: str) -> Path:
    """Save stage output to the research directory."""
    path = RESEARCH_DIR / filename
    path.write_text(content, encoding="utf-8")
    log.info("✓ Saved: %s (%d bytes)", path.name, len(content))
    return path


def _load_output(filename: str) -> str | None:
    """Load a previously saved stage output."""
    path = RESEARCH_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _context_msg(role: str, content: str) -> dict:
    """Build a context injection message."""
    return {"role": "user", "content": f"[Context from {role}]:\n\n{content}"}


# ── Stage implementations ─────────────────────────────────────────────────────

def run_economist(runner: AgentRunner) -> str:
    """Stage 0: Economist produces the daily macro briefing."""
    log.info("═══ Stage 0: ECONOMIST ═══")

    user_msg = (
        f"Today is {TODAY}. Produce your daily macroeconomic briefing.\n\n"
        "Perform a fresh web search using your native capabilities to read "
        "live data from the designated sources. Write the briefing following "
        "the structure defined in your system prompt."
    )

    response = runner.run("economist", user_msg, temperature=0.3)
    _save_output(f"economist_briefing_{TODAY}.md", response)
    return response


def run_researcher(runner: AgentRunner, economist_briefing: str) -> str:
    """Stage 1: Researcher scans news and surfaces opportunities."""
    log.info("═══ Stage 1: RESEARCHER ═══")

    log.info("Fetching today's Yahoo Finance articles...")
    
    articles_data = None
    for attempt in range(6):
        scraper_result = _run_scraper({"max_articles": 20})
        if scraper_result.get("success") and "data" in scraper_result:
            articles_list = scraper_result["data"].get("articles", [])
            num_articles = len(articles_list)
            if num_articles >= 15:
                log.info("Successfully scraped %d articles.", num_articles)
                articles_data = json.dumps(scraper_result["data"], ensure_ascii=False)
                break
            else:
                log.warning("Scraper returned %d articles (less than 15).", num_articles)
        else:
            log.warning("Scraper failed or returned no data: %s", scraper_result)
            
        if attempt < 5:
            log.info("Retrying scraper (Attempt %d/5)...", attempt + 1)
            
    if articles_data is None:
        error_msg = "Scraper failed to return at least 15 articles after 5 retries. Stopping."
        log.error(error_msg)
        raise RuntimeError(error_msg)

    user_msg = (
        f"Today is {TODAY}. I have scraped today's Yahoo Finance articles for you.\n\n"
        f"Articles Data:\n{articles_data}\n\n"
        "Analyze these results and surface investment opportunities as defined in your system prompt."
    )

    context = [_context_msg("Economist", economist_briefing)]
    response = runner.run("researcher", user_msg, context=context)
    _save_output(f"researcher_daily_{TODAY}.md", response)
    return response


def run_analyst(
    runner: AgentRunner,
    economist_briefing: str,
    researcher_output: str,
) -> str:
    """Stage 2: Analyst performs financial analysis on surfaced opportunities."""
    log.info("═══ Stage 2: ANALYST ═══")

    user_msg = (
        f"Today is {TODAY}. Analyze the opportunities surfaced by the "
        "Researcher below. For each promising opportunity:\n"
        "1. Pull market data using the pull_market_data tool\n"
        "2. Run standard models (DCF, technicals, peer comparison, scenario)\n"
        "3. Assess macro consistency with the Economist's briefing\n"
        "4. Write your structured analyst report\n\n"
        "Provide your per-ticker analysis and summary in your final response."
    )

    context = [
        _context_msg("Economist", economist_briefing),
        _context_msg("Researcher", researcher_output),
    ]

    response = runner.run(
        "analyst", user_msg, context=context, max_tokens=32768,
        max_tool_rounds=30,
    )
    _save_output(f"analyst_summary_{TODAY}.md", response)
    return response


def run_associates(
    runner: AgentRunner,
    economist_briefing: str,
    analyst_output: str,
) -> dict[str, str]:
    """Stage 3: Three Associates independently build scenarios."""
    log.info("═══ Stage 3: ASSOCIATES (3 scenarios) ═══")

    user_msg = (
        f"Today is {TODAY}. Build your independent scenario for the "
        "opportunity analyzed by the Analyst. Follow your system prompt "
        "structure exactly. Include macro regime alignment from the "
        "Economist's briefing."
    )

    context = [
        _context_msg("Economist", economist_briefing),
        _context_msg("Analyst", analyst_output),
    ]

    roles = [
        "base_associate",
        "bear_associate",
        "bull_associate",
    ]

    # Map role to output filename
    role_to_file = {
        "base_associate": f"associate_base_{TODAY}.md",
        "bear_associate": f"associate_bear_{TODAY}.md",
        "bull_associate": f"associate_bull_{TODAY}.md",
    }

    results = runner.run_batch(roles, user_msg, context=context, temperature=0.5)

    for role, text in results.items():
        _save_output(role_to_file[role], text)

    return results


def run_manager(
    runner: AgentRunner,
    economist_briefing: str,
    analyst_output: str,
    associate_outputs: dict[str, str],
) -> str:
    """Stage 4: Manager makes the final investment decision."""
    log.info("═══ Stage 4: MANAGER ═══")

    # Consolidate all associate scenarios into one context block
    scenarios_text = ""
    for role, text in sorted(associate_outputs.items()):
        label = role.replace("_", " ").title()
        scenarios_text += f"\n\n--- {label} ---\n\n{text}"

    user_msg = (
        f"Today is {TODAY}. Review all prior output from the pipeline — "
        "the Economist's macro briefing, the Analyst's report, and all three "
        "Associate scenarios — and make your investment decision.\n\n"
        "If your decision is BUY:\n"
        "- Use the execute_trade tool to place the order\n"
        "- Include structured sell_conditions\n"
        "- Check the portfolio first with get_portfolio\n\n"
        "If your decision is SELL:\n"
        "- Use the execute_trade tool to execute"
    )

    context = [
        _context_msg("Economist", economist_briefing),
        _context_msg("Analyst", analyst_output),
        _context_msg("Associates (3 Scenarios)", scenarios_text),
    ]

    response = runner.run("manager", user_msg, context=context, max_tokens=16384)
    _save_output(f"manager_decision_{TODAY}.md", response)
    return response


# ── Full pipeline orchestrator ────────────────────────────────────────────────

STAGES = ["economist", "researcher", "analyst", "associates", "manager"]


def run_pipeline(start_stage: str = "economist", dry_run: bool = False) -> None:
    """
    Run the full investment research pipeline.

    Parameters
    ----------
    start_stage : str
        Stage to start from. Prior stages' outputs are loaded from disk.
    dry_run : bool
        If True, only print what would run.
    """
    stage_idx = STAGES.index(start_stage) if start_stage in STAGES else 0

    if dry_run:
        print(f"Pipeline would run stages: {STAGES[stage_idx:]}")
        print(f"Date: {TODAY}")
        print(f"Research dir: {RESEARCH_DIR}")
        return

    client = LLMClient()
    runner = AgentRunner(client)

    try:
        current_stage = "Economist"
        # ── Economist ─────────────────────────────────────────────────────
        if stage_idx <= 0:
            economist_briefing = run_economist(runner)
        else:
            economist_briefing = _load_output(f"economist_briefing_{TODAY}.md")
            if not economist_briefing:
                error_msg = f"No economist briefing found for {TODAY}. Run economist first."
                log.error(error_msg)
                raise RuntimeError(error_msg)
            log.info("Loaded existing economist briefing for %s", TODAY)

        current_stage = "Researcher"
        # ── Researcher ────────────────────────────────────────────────────
        if stage_idx <= 1:
            researcher_output = run_researcher(runner, economist_briefing)
        else:
            researcher_output = _load_output(f"researcher_daily_{TODAY}.md")
            if not researcher_output:
                error_msg = f"No researcher output found for {TODAY}. Run researcher first."
                log.error(error_msg)
                raise RuntimeError(error_msg)
            log.info("Loaded existing researcher output for %s", TODAY)

        current_stage = "Analyst"
        # ── Analyst ───────────────────────────────────────────────────────
        if stage_idx <= 2:
            analyst_output = run_analyst(runner, economist_briefing, researcher_output)
        else:
            analyst_output = _load_output(f"analyst_summary_{TODAY}.md")
            if not analyst_output:
                error_msg = f"No analyst output found for {TODAY}. Run analyst first."
                log.error(error_msg)
                raise RuntimeError(error_msg)
            log.info("Loaded existing analyst output for %s", TODAY)

        current_stage = "Associates"
        # ── Associates ────────────────────────────────────────────────────
        if stage_idx <= 3:
            associate_outputs = run_associates(runner, economist_briefing, analyst_output)
        else:
            associate_outputs = {}
            label_to_role = {
                "base": "base_associate",
                "bear": "bear_associate",
                "bull": "bull_associate",
            }
            for label, role in label_to_role.items():
                text = _load_output(f"associate_{label}_{TODAY}.md")
                if text:
                    associate_outputs[role] = text
            if not associate_outputs:
                error_msg = f"No associate outputs found for {TODAY}. Run associates first."
                log.error(error_msg)
                raise RuntimeError(error_msg)
            log.info("Loaded %d existing associate outputs for %s", len(associate_outputs), TODAY)

        current_stage = "Manager"
        # ── Manager ───────────────────────────────────────────────────────
        if stage_idx <= 4:
            manager_decision = run_manager(
                runner, economist_briefing, analyst_output, associate_outputs
            )
            log.info("═══ Pipeline complete. Manager decision saved. ═══")

    except Exception as exc:
        raise RuntimeError(f"[{current_stage} Stage] {exc}") from exc
    finally:
        client.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        prog="pipeline",
        description="AutoFund — Daily Research Pipeline",
    )
    parser.add_argument(
        "--stage", "-s",
        choices=STAGES,
        default="economist",
        help="Stage to start from (prior outputs loaded from disk)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would run without executing",
    )

    args = parser.parse_args()
    run_pipeline(start_stage=args.stage, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
