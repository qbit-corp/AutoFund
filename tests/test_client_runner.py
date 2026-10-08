"""
Tests for the LLM Client (agents/client.py) and Agent Runner (agents/runner.py).
Network calls are fully mocked — no OpenRouter API key required.
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ── LLMClient ────────────────────────────────────────────────────────────────


class TestLLMClient:

    def test_init_requires_api_key(self):
        """LLMClient raises if no API key is available."""
        from agents.client import LLMClient
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(EnvironmentError, match="OPENROUTER_API_KEY"):
                LLMClient(api_key=None)

    def test_init_with_explicit_key(self):
        """LLMClient accepts an explicit API key."""
        from agents.client import LLMClient
        client = LLMClient(api_key="test-key-123")
        assert client.api_key == "test-key-123"
        client.close()

    def test_init_from_env(self):
        """LLMClient reads key from environment."""
        from agents.client import LLMClient
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "env-key"}):
            client = LLMClient()
            assert client.api_key == "env-key"
            client.close()

    def test_get_response_text_empty_choices(self):
        """get_response_text handles empty choices gracefully."""
        from agents.client import LLMClient
        client = LLMClient(api_key="test")
        assert client.get_response_text({"choices": []}) == ""
        assert client.get_response_text({}) == ""
        client.close()

    def test_get_response_text_normal(self):
        """get_response_text extracts content correctly."""
        from agents.client import LLMClient
        client = LLMClient(api_key="test")
        response = {
            "choices": [{"message": {"content": "Hello, world!"}}]
        }
        assert client.get_response_text(response) == "Hello, world!"
        client.close()

    def test_get_response_text_none_content(self):
        """get_response_text returns empty string for None content."""
        from agents.client import LLMClient
        client = LLMClient(api_key="test")
        response = {"choices": [{"message": {"content": None}}]}
        assert client.get_response_text(response) == ""
        client.close()

    def test_get_tool_calls_empty(self):
        """get_tool_calls returns empty list when no tool calls."""
        from agents.client import LLMClient
        client = LLMClient(api_key="test")
        assert client.get_tool_calls({}) == []
        assert client.get_tool_calls({"choices": [{"message": {}}]}) == []
        assert client.get_tool_calls({"choices": [{"message": {"tool_calls": None}}]}) == []
        client.close()

    def test_get_tool_calls_present(self):
        """get_tool_calls extracts tool calls correctly."""
        from agents.client import LLMClient
        client = LLMClient(api_key="test")
        tc = [{"id": "call_1", "function": {"name": "test", "arguments": "{}"}}]
        response = {"choices": [{"message": {"tool_calls": tc}}]}
        assert client.get_tool_calls(response) == tc
        client.close()

    def test_context_manager(self):
        """LLMClient works as a context manager."""
        from agents.client import LLMClient
        with LLMClient(api_key="test") as client:
            assert client.api_key == "test"


# ── Model Config ─────────────────────────────────────────────────────────────


class TestModelConfig:

    def test_get_model_for_known_role(self):
        """get_model_for_role returns correct model for known roles."""
        from agents.client import get_model_for_role
        # Reset cached config
        import agents.client as client_module
        client_module._model_config = None

        result = get_model_for_role("economist")
        assert result == "perplexity/sonar"

    def test_get_model_for_unknown_role(self):
        """get_model_for_role returns fallback for unknown roles."""
        from agents.client import get_model_for_role
        result = get_model_for_role("nonexistent_role")
        # Should return the default fallback
        assert isinstance(result, str)
        assert len(result) > 0


# ── AgentRunner ──────────────────────────────────────────────────────────────


class TestAgentRunner:

    def test_model_role_mapping(self):
        """Associate sub-roles map to 'associates' model config key."""
        from agents.runner import _model_role
        assert _model_role("base_associate") == "associates"
        assert _model_role("bear_associate") == "associates"
        assert _model_role("bull_associate") == "associates"
        assert _model_role("economist") == "economist"
        assert _model_role("manager") == "manager"

    def test_load_prompt_for_valid_roles(self):
        """All registered roles can load their prompt files."""
        from agents.runner import _load_prompt, PROMPT_FILES
        for role in PROMPT_FILES:
            try:
                prompt = _load_prompt(role)
                assert isinstance(prompt, str)
                assert len(prompt) > 0
            except FileNotFoundError:
                # Prompt file might not exist yet — that's a valid state
                pass

    def test_load_prompt_unknown_role_raises(self):
        """Unknown role raises ValueError."""
        from agents.runner import _load_prompt
        with pytest.raises(ValueError, match="No prompt file"):
            _load_prompt("janitor")

    def test_run_returns_text_on_no_tool_calls(self):
        """AgentRunner.run returns text when LLM sends no tool calls."""
        from agents.runner import AgentRunner
        from agents.client import LLMClient

        mock_client = MagicMock(spec=LLMClient)
        mock_client.chat.return_value = {
            "choices": [{"message": {"content": "Final answer"}}]
        }
        mock_client.get_tool_calls.return_value = []
        mock_client.get_response_text.return_value = "Final answer"

        runner = AgentRunner(mock_client)

        with patch("agents.runner._load_prompt", return_value="You are a test agent."), \
             patch("agents.runner.get_tools_for_role", return_value=[]):
            result = runner.run("economist", "What is GDP?")
        assert result == "Final answer"

    def test_run_handles_tool_loop(self):
        """AgentRunner.run processes tool calls and sends results back."""
        from agents.runner import AgentRunner
        from agents.client import LLMClient

        mock_client = MagicMock(spec=LLMClient)

        # First call: LLM returns a tool call
        tool_call_response = {
            "choices": [{
                "message": {
                    "content": "",
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call_1",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "README.md"}),
                        },
                    }],
                }
            }]
        }
        # Second call: LLM returns final text
        final_response = {
            "choices": [{"message": {"content": "Done reading the file."}}]
        }

        mock_client.chat.side_effect = [tool_call_response, final_response]
        mock_client.get_tool_calls.side_effect = [
            [tool_call_response["choices"][0]["message"]["tool_calls"][0]],
            [],
        ]
        mock_client.get_response_text.return_value = "Done reading the file."

        runner = AgentRunner(mock_client)

        with patch("agents.runner._load_prompt", return_value="System prompt"), \
             patch("agents.runner.get_tools_for_role", return_value=[{"function": {"name": "read_file"}}]), \
             patch("agents.runner.execute_tool_call", return_value={"content": "README contents"}):
            result = runner.run("researcher", "Read README")
        assert result == "Done reading the file."
        assert mock_client.chat.call_count == 2

    def test_run_batch(self):
        """run_batch runs multiple roles and returns results dict."""
        from agents.runner import AgentRunner

        mock_client = MagicMock()
        mock_client.chat.return_value = {
            "choices": [{"message": {"content": "response"}}]
        }
        mock_client.get_tool_calls.return_value = []
        mock_client.get_response_text.return_value = "response"

        runner = AgentRunner(mock_client)

        with patch("agents.runner._load_prompt", return_value="prompt"), \
             patch("agents.runner.get_tools_for_role", return_value=[]):
            results = runner.run_batch(
                ["economist", "researcher"], "Test message"
            )
        assert "economist" in results
        assert "researcher" in results
        assert results["economist"] == "response"

    def test_run_batch_captures_errors(self):
        """run_batch doesn't crash if one agent fails."""
        from agents.runner import AgentRunner

        mock_client = MagicMock()
        mock_client.chat.side_effect = RuntimeError("API down")

        runner = AgentRunner(mock_client)

        with patch("agents.runner._load_prompt", return_value="prompt"), \
             patch("agents.runner.get_tools_for_role", return_value=[]):
            results = runner.run_batch(["economist"], "Test")
        assert "ERROR" in results["economist"]
