"""
AutoFund — Agent Runner
========================
Executes a single agent: loads its system prompt, sends messages to the LLM,
handles multi-turn tool calls, and returns the final text response.

This is the core execution loop that powers every stage of the pipeline.
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.client import LLMClient, get_model_for_role, get_provider_for_role
from agents.tools import get_tools_for_role, execute_tool_call

log = logging.getLogger("agents.runner")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"

# Which prompt file belongs to which role
PROMPT_FILES: dict[str, str] = {
    "economist": "01_economist.md",
    "researcher": "00_researcher.md",
    "analyst": "02_analyst.md",
    "base_associate": "03a_base_associates.md",
    "bear_associate": "03b_bearish_associates.md",
    "bull_associate": "03c_bullish_associates.md",
    "manager": "04_manager.md",
}


def _load_prompt(role: str) -> str:
    """Load the system prompt for ``role``."""
    filename = PROMPT_FILES.get(role, "")
    if not filename:
        raise ValueError(f"No prompt file registered for role '{role}'.")
    path = PROMPTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def _model_role(role: str) -> str:
    """Map specific associate sub-roles to their model config key."""
    if "associate" in role:
        return "associates"
    return role


# ── Tool-call XML sanitizer ──────────────────────────────────────────────────
# Some models (e.g. minimax) emit tool-call markup in their text even when the
# request has tools=None.  These regexes catch the known patterns so leaked
# tool calls don't pollute the agent's saved output.

_TOOL_CALL_XML_PATTERNS = [
    # minimax native:  <minimax:tool_call>…</minimax:tool_call>
    re.compile(r"<minimax:tool_call>.*?</minimax:tool_call>", re.DOTALL),
    # Generic XML tool call patterns other models might use
    re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL),
    re.compile(r"<function_call>.*?</function_call>", re.DOTALL),
    # Inline invoke patterns:  <invoke name="…">…</invoke>
    re.compile(r"<invoke\b[^>]*>.*?</invoke>", re.DOTALL),
]


def _strip_tool_call_xml(text: str) -> str:
    """Remove model-native tool-call XML from a text response."""
    for pattern in _TOOL_CALL_XML_PATTERNS:
        text = pattern.sub("", text)
    return text.strip()


class AgentRunner:
    """
    Run a single agent through its system prompt + user message,
    handling tool calls in a loop until the agent returns a final text.

    Parameters
    ----------
    client : LLMClient
        The shared LLM client instance.
    max_tool_rounds : int
        Safety limit on tool call iterations (default: 10).
    """

    def __init__(self, client: LLMClient, max_tool_rounds: int = 10):
        self.client = client
        self.max_tool_rounds = max_tool_rounds

    def run(
        self,
        role: str,
        user_message: str,
        context: list[dict] | None = None,
        temperature: float = 0.4,
        max_tokens: int = 16384,
        max_tool_rounds: int | None = None,
    ) -> str:
        """
        Execute an agent.

        Parameters
        ----------
        role : str
            Agent role (e.g. ``"economist"``, ``"analyst"``, ``"manager"``).
        user_message : str
            The user/pipeline message that kicks off the agent.
        context : list[dict], optional
            Additional messages to inject between system and user
            (e.g. prior stage output, economist briefing).
        temperature : float
            LLM temperature.
        max_tokens : int
            Max response tokens.
        max_tool_rounds : int, optional
            Override the instance-level tool round limit for this call.

        Returns
        -------
        str
            The agent's final text response.
        """
        effective_max_rounds = max_tool_rounds if max_tool_rounds is not None else self.max_tool_rounds
        model = get_model_for_role(_model_role(role))
        provider = get_provider_for_role(_model_role(role))
        system_prompt = _load_prompt(role)
        tools = get_tools_for_role(_model_role(role))

        kwargs = {}
        if provider is not None:
            kwargs["provider"] = provider

        # Build message chain
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
        ]
        if context:
            messages.extend(context)
        messages.append({"role": "user", "content": user_message})

        log.info("─── Agent [%s] starting  |  model=%s ───", role, model)

        for round_idx in range(effective_max_rounds):
            response = self.client.chat(
                model=model,
                messages=messages,
                tools=tools if tools else None,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )

            tool_calls = self.client.get_tool_calls(response)

            if not tool_calls:
                # No tool calls — this is the final response
                text = self.client.get_response_text(response)
                log.info(
                    "─── Agent [%s] finished  |  rounds=%d  |  len=%d chars ───",
                    role, round_idx + 1, len(text),
                )
                return text

            # Process tool calls
            # Add assistant message with tool calls.
            # DeepSeek thinking models (via OpenRouter) return the reasoning
            # trace in `message.reasoning`. It must be echoed back as
            # `reasoning_content` in the next request, or the API returns 400.
            raw_msg = response["choices"][0]["message"]
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": raw_msg.get("content") or "",
                "tool_calls": raw_msg.get("tool_calls", []),
            }
            # OpenRouter field name → DeepSeek API field name
            reasoning = raw_msg.get("reasoning") or raw_msg.get("reasoning_content")
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            messages.append(assistant_msg)

            for tc in tool_calls:
                func_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                log.info(
                    "  Tool call [%s]: %s(%s)",
                    role, func_name, json.dumps(args)[:200],
                )

                result = execute_tool_call(func_name, args)

                # Serialize result for the model
                result_str = json.dumps(result, default=str, ensure_ascii=False)
                # Truncate very large results to avoid token overflow
                if len(result_str) > 50_000:
                    result_str = result_str[:50_000] + "\n... [TRUNCATED — result too large]"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_str,
                })

        # Exhausted tool rounds — force a final response without tools
        log.warning("Agent [%s] exhausted %d tool rounds. Forcing text response.", role, effective_max_rounds)
        response = self.client.chat(
            model=model,
            messages=messages,
            tools=None,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs
        )
        text = self.client.get_response_text(response)
        text = _strip_tool_call_xml(text)
        if not text.strip():
            log.warning(
                "Agent [%s] returned empty text after stripping leaked tool calls. "
                "The agent likely needed more tool rounds.",
                role,
            )
            text = (
                f"[Agent '{role}' exhausted its {effective_max_rounds} tool rounds "
                f"and could not produce a final answer. "
                f"Consider increasing max_tool_rounds for this agent.]"
            )
        log.info(
            "─── Agent [%s] finished (forced)  |  rounds=%d  |  len=%d chars ───",
            role, effective_max_rounds + 1, len(text),
        )
        return text

    def run_batch(
        self,
        roles: list[str],
        user_message: str,
        context: list[dict] | None = None,
        temperature: float = 0.4,
    ) -> dict[str, str]:
        """
        Run multiple agents **in parallel** with the same input.
        Returns a dict mapping role → response text.

        All API calls are fired concurrently so the upstream LLM provider
        can process them simultaneously, reducing wall-clock time from
        ~sum(all calls) to ~max(single call).

        Used for the Associates stage where agents run independently.
        """
        results: dict[str, str] = {}
        log.info("Launching %d agents in parallel: %s", len(roles), roles)

        with ThreadPoolExecutor(max_workers=len(roles)) as executor:
            future_to_role = {
                executor.submit(
                    self.run, role, user_message,
                    context=context, temperature=temperature,
                ): role
                for role in roles
            }

            for future in as_completed(future_to_role):
                role = future_to_role[future]
                try:
                    results[role] = future.result()
                except Exception as e:
                    log.error("Agent [%s] failed: %s", role, e, exc_info=True)
                    results[role] = f"ERROR: {type(e).__name__}: {e}"

        log.info("All %d agents completed.", len(roles))
        return results
