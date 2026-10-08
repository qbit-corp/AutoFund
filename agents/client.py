"""
AutoFund — LLM Client (OpenRouter)
====================================
Thin wrapper around the OpenAI-compatible endpoint exposed by OpenRouter.
All agents access LLMs through this shared client.

Requires the environment variable ``OPENROUTER_API_KEY``.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("agents.client")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# ── Model configuration ──────────────────────────────────────────────────────

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "models.json"


def _load_model_config() -> dict[str, Any]:
    """Load model assignments from ``config/models.json``."""
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    log.warning("models.json not found at %s — using defaults.", _CONFIG_PATH)
    return {
        "economist": {"model": "perplexity/sonar"},
        "researcher": {"model": "qwen/qwen3-235b-a22b-2507", "provider": "deepinfra"},
        "analyst": {"model": "deepseek/deepseek-v4-flash", "provider": "deepseek"},
        "associates": {"model": "qwen/qwen3-235b-a22b-2507", "provider": "deepinfra"},
        "manager": {"model": "deepseek/deepseek-v4-flash", "provider": "deepseek"},
    }


_model_config: dict[str, Any] | None = None


def get_model_for_role(role: str) -> str:
    """Return the model ID for the given agent *role*."""
    global _model_config
    if _model_config is None:
        _model_config = _load_model_config()
    
    config = _model_config.get(role, {})
    if isinstance(config, str):
        return config
    return config.get("model", "qwen/qwen3-235b-a22b-2507")

def get_provider_for_role(role: str) -> Any:
    """Return the provider for the given agent *role*."""
    global _model_config
    if _model_config is None:
        _model_config = _load_model_config()
    
    config = _model_config.get(role, {})
    if isinstance(config, str):
        return None
    return config.get("provider")


# ── LLM Client ───────────────────────────────────────────────────────────────

class LLMClient:
    """
    OpenRouter-compatible LLM client with tool-use support.

    Parameters
    ----------
    api_key : str, optional
        OpenRouter API key.  Falls back to ``OPENROUTER_API_KEY`` env var.
    base_url : str, optional
        Base URL for the API.  Defaults to OpenRouter.
    timeout : float
        Request timeout in seconds (default: 300 — models can be slow).
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = OPENROUTER_BASE,
        timeout: float = 300.0,
    ):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise EnvironmentError(
                "OPENROUTER_API_KEY is not set. Set it via environment variable "
                "or pass api_key= to LLMClient()."
            )
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    # ── Core chat completion ──────────────────────────────────────────────

    def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | dict = "auto",
        temperature: float = 0.4,
        max_tokens: int = 16384,
        **kwargs: Any,
    ) -> dict:
        """
        Send a chat completion request.

        Parameters
        ----------
        model : str
            OpenRouter model ID (e.g. ``"qwen/qwen3-235b-a22b-2507"``).
        messages : list[dict]
            Conversation messages in OpenAI format.
        tools : list[dict], optional
            Tool/function definitions in OpenAI format.
        tool_choice : str | dict
            How to handle tools — ``"auto"``, ``"none"``, or a specific tool.
        temperature : float
            Sampling temperature.
        max_tokens : int
            Maximum response tokens.

        Returns
        -------
        dict
            The full API response body.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://autofund.local",
            "X-Title": "AutoFund Investment Pipeline",
        }

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }

        if "provider" in payload:
            if isinstance(payload["provider"], str) and payload["provider"]:
                payload["provider"] = {"order": [payload["provider"]], "allow_fallbacks": False}
            elif not payload["provider"]:
                del payload["provider"]
        else:
            if "qwen/" in model.lower():
                payload["provider"] = {"order": ["deepinfra"], "allow_fallbacks": False}
            elif "deepseek/" in model.lower():
                payload["provider"] = {"order": ["deepseek"], "allow_fallbacks": False}

        # DeepSeek thinking models require reasoning tokens to be returned so
        # they can be echoed back in multi-turn conversations (400 otherwise).
        if "deepseek/" in model.lower():
            payload["include_reasoning"] = True

        # Perplexity models do not support tool use in OpenRouter
        if tools and "perplexity/" not in model.lower():
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        url = f"{self.base_url}/chat/completions"

        log.info("→ %s  |  model=%s  |  msgs=%d", url, model, len(messages))

        max_retries = 5
        base_delay = 5.0

        for attempt in range(max_retries):
            try:
                resp = self._client.post(url, headers=headers, json=payload)

                if resp.status_code == 200:
                    break

                log.error(
                    "OpenRouter error %d (attempt %d/%d): %s",
                    resp.status_code, attempt + 1, max_retries, resp.text[:500]
                )

                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt < max_retries - 1:
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            delay = float(retry_after)
                        else:
                            delay = base_delay * (2 ** attempt)
                        log.warning("Rate limited/Server error. Retrying in %.1f seconds...", delay)
                        time.sleep(delay)
                        continue

                resp.raise_for_status()

            except httpx.RequestError as e:
                log.error("Network error (attempt %d/%d): %s", attempt + 1, max_retries, e)
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    log.warning("Network issue. Retrying in %.1f seconds...", delay)
                    time.sleep(delay)
                    continue
                raise

        data = resp.json()
        log.info(
            "← Response: %d tokens (prompt=%s, completion=%s)",
            data.get("usage", {}).get("total_tokens", 0),
            data.get("usage", {}).get("prompt_tokens", "?"),
            data.get("usage", {}).get("completion_tokens", "?"),
        )
        return data

    def get_response_text(self, response: dict) -> str:
        """Extract the assistant's text from a chat completion response."""
        choices = response.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        return message.get("content", "") or ""

    def get_tool_calls(self, response: dict) -> list[dict]:
        """Extract tool calls from a chat completion response."""
        choices = response.get("choices", [])
        if not choices:
            return []
        message = choices[0].get("message", {})
        return message.get("tool_calls", []) or []

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
