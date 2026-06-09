"""Tests for ModelTier abstraction and provider resolution."""

from unittest.mock import MagicMock, patch

import pytest

from caw.agent import Agent
from caw.models import ModelTier, Trajectory
from caw.providers.claude_code import ClaudeCodeProvider
from caw.providers.codex import CodexProvider


class TestModelTierResolution:
    def test_claude_code_strongest(self):
        provider = ClaudeCodeProvider()
        assert provider.resolve_model(ModelTier.STRONGEST) == "opus"

    def test_claude_code_fast(self):
        provider = ClaudeCodeProvider()
        assert provider.resolve_model(ModelTier.FAST) == "claude-haiku-4-5-20251001"

    def test_codex_strongest(self):
        provider = CodexProvider()
        assert provider.resolve_model(ModelTier.STRONGEST) is None

    def test_codex_fast(self):
        provider = CodexProvider()
        assert provider.resolve_model(ModelTier.FAST) == "gpt-5.3-codex-spark"

    def test_base_provider_raises(self):
        from caw.provider import Provider

        class BareProvider(Provider):
            @property
            def name(self):
                return "bare"

            def start_session(self, mcp_servers, **kwargs):
                pass

        provider = BareProvider()
        with pytest.raises(NotImplementedError):
            provider.resolve_model(ModelTier.STRONGEST)

    def test_agent_resolves_tier_in_start_session(self):
        agent = Agent(provider="claude_code", model=ModelTier.STRONGEST, data_dir=None)

        mock_session = MagicMock()
        mock_session.trajectory = Trajectory(agent="claude_code")

        with patch.object(ClaudeCodeProvider, "start_session", return_value=mock_session) as mock_start:
            agent.start_session()
            call_kwargs = mock_start.call_args
            assert call_kwargs.kwargs.get("model") == "opus"

    def test_agent_leaves_codex_strongest_to_provider_default(self):
        agent = Agent(provider="codex", model=ModelTier.STRONGEST, data_dir=None)

        mock_session = MagicMock()
        mock_session.trajectory = Trajectory(agent="codex")

        with patch.object(CodexProvider, "start_session", return_value=mock_session) as mock_start:
            agent.start_session()
            call_kwargs = mock_start.call_args
            assert call_kwargs.kwargs.get("model") is None

    def test_agent_passes_string_model_unchanged(self):
        agent = Agent(provider="claude_code", model="my-custom-model", data_dir=None)

        mock_session = MagicMock()
        mock_session.trajectory = Trajectory(agent="claude_code")

        with patch.object(ClaudeCodeProvider, "start_session", return_value=mock_session) as mock_start:
            agent.start_session()
            call_kwargs = mock_start.call_args
            assert call_kwargs.kwargs.get("model") == "my-custom-model"
