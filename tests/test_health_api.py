"""Tests for the provider health/availability API (check_health, check_providers)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from caw.health import (
    AuthSignal,
    ProviderHealth,
    check_providers,
    claude_auth_signal,
)
from caw.provider import Provider


# ---------------------------------------------------------------------------
# A minimal concrete provider for exercising the base-class health logic
# without depending on any real CLI being installed.
# ---------------------------------------------------------------------------


class DummyProvider(Provider):
    def __init__(self, *, binary: str = "dummy-bin", limit=None, raise_exc=None):
        self._binary = binary
        self._limit = limit
        self._raise_exc = raise_exc

    @property
    def name(self) -> str:
        return "dummy"

    @property
    def binary_name(self) -> str:
        return self._binary

    def start_session(self, mcp_servers, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError

    def check_limit(self, model=None):
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._limit


@pytest.fixture
def fake_which(monkeypatch):
    """Make shutil.which resolve only the names we register."""
    installed: dict[str, str] = {}

    def _which(name):
        return installed.get(name)

    monkeypatch.setattr(shutil, "which", _which)
    return installed


# ---------------------------------------------------------------------------
# Binary detection (fast check)
# ---------------------------------------------------------------------------


class TestBinaryDetection:
    def test_not_installed(self, fake_which):
        h = DummyProvider().check_health()
        assert isinstance(h, ProviderHealth)
        assert h.installed is False
        assert h.binary_path is None

    def test_installed(self, fake_which):
        fake_which["dummy-bin"] = "/opt/dummy-bin"
        h = DummyProvider().check_health()
        assert h.installed is True
        assert h.binary_path == "/opt/dummy-bin"

    def test_fast_check_does_not_probe(self, fake_which):
        fake_which["dummy-bin"] = "/opt/dummy-bin"
        h = DummyProvider(limit=42).check_health()  # live defaults to False
        assert h.probed is False
        assert h.rate_limited is None
        assert h.wait_minutes is None


# ---------------------------------------------------------------------------
# Live probe
# ---------------------------------------------------------------------------


class TestLiveProbe:
    def test_responds_not_limited(self, fake_which):
        fake_which["dummy-bin"] = "/opt/dummy-bin"
        h = DummyProvider(limit=None).check_health(live=True)
        assert h.probed is True
        assert h.rate_limited is False
        assert h.wait_minutes is None
        assert h.error is None

    def test_rate_limited(self, fake_which):
        fake_which["dummy-bin"] = "/opt/dummy-bin"
        h = DummyProvider(limit=30).check_health(live=True)
        assert h.probed is True
        assert h.rate_limited is True
        assert h.wait_minutes == 30

    def test_probe_error_is_captured_not_raised(self, fake_which):
        fake_which["dummy-bin"] = "/opt/dummy-bin"
        h = DummyProvider(raise_exc=RuntimeError("boom")).check_health(live=True)
        assert h.probed is True
        assert h.error == "boom"
        assert h.rate_limited is None

    def test_live_skipped_when_not_installed(self, fake_which):
        h = DummyProvider(limit=30).check_health(live=True)
        assert h.installed is False
        assert h.probed is False
        assert h.rate_limited is None


# ---------------------------------------------------------------------------
# check_providers sweep
# ---------------------------------------------------------------------------


class TestCheckProviders:
    def test_default_lists_distinct_providers(self):
        healths = check_providers()
        names = [h.provider for h in healths]
        # built-in providers, deduped across aliases
        assert names == ["claude_code", "codex", "opencode"]

    def test_named_subset_dedups_aliases(self):
        healths = check_providers(["claude", "cc", "claude_code", "codex"])
        assert [h.provider for h in healths] == ["claude_code", "codex"]

    def test_unknown_name_raises(self):
        with pytest.raises(ValueError, match="Unknown provider 'nope'"):
            check_providers(["nope"])


# ---------------------------------------------------------------------------
# Auth introspection (raw signals, no verdict)
# ---------------------------------------------------------------------------


def _write_claude_creds(home: Path, expires_at_ms: int) -> None:
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "t@example.com", "organizationName": "Org"}})
    )
    claude_dir = home / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": expires_at_ms}})
    )


class TestClaudeAuthSignal:
    def test_valid_token(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        _write_claude_creds(home, expires_at_ms=9999999999000)  # far future
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        sig = claude_auth_signal()
        assert isinstance(sig, AuthSignal)
        assert sig.present is True
        assert sig.token_expired is False
        assert sig.token_expires_at is not None

    def test_expired_token(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        _write_claude_creds(home, expires_at_ms=1000)  # 1970 → expired
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        sig = claude_auth_signal()
        assert sig.present is True
        assert sig.token_expired is True

    def test_no_credentials(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        sig = claude_auth_signal()
        assert sig.present is False
        assert "no credentials" in sig.detail

    def test_env_var_counts_as_present(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()  # no creds file
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        sig = claude_auth_signal()
        assert sig.present is True
        assert "ANTHROPIC_API_KEY" in sig.detail
