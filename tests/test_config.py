"""Tests for ~/.caw model configuration (caw.config) and the `caw config` CLI."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from caw import config
from caw.config_cli import app as config_app
from caw.models import ModelTier


runner = CliRunner()


# --- baked-in defaults ------------------------------------------------------


def test_baked_defaults_resolution():
    assert config.get_model("claude_code", ModelTier.STRONGEST) == "opus"
    assert config.get_model("claude_code", ModelTier.FAST) == "claude-haiku-4-5-20251001"
    assert config.get_model("codex", ModelTier.STRONGEST) is None
    assert config.get_model("codex", ModelTier.FAST) == "gpt-5.3-codex-spark"
    assert config.get_model("opencode", "strongest") == "openai/gpt-5.6-sol"


def test_alias_resolves_to_canonical():
    assert config.canonical_provider("claude") == "claude_code"
    assert config.canonical_provider("cc") == "claude_code"
    assert config.get_model("claude", ModelTier.STRONGEST) == "opus"


# --- precedence -------------------------------------------------------------


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "opus-from-env")
    assert config.get_model("claude_code", ModelTier.STRONGEST) == "opus-from-env"


def test_user_config_over_baked():
    config.set_user_model("claude_code", ModelTier.STRONGEST, "my-opus")
    assert config.get_model("claude_code", ModelTier.STRONGEST) == "my-opus"
    # FAST is untouched, still baked.
    assert config.get_model("claude_code", ModelTier.FAST) == "claude-haiku-4-5-20251001"


def test_env_over_user_config(monkeypatch):
    config.set_user_model("claude_code", ModelTier.STRONGEST, "my-opus")
    monkeypatch.setenv("ANTHROPIC_MODEL", "opus-from-env")
    assert config.get_model("claude_code", ModelTier.STRONGEST) == "opus-from-env"


# --- set / unset persistence ------------------------------------------------


def test_set_writes_file_and_unset_reverts():
    config.set_user_model("opencode", "fast", "openai/custom-fast")
    on_disk = json.loads(config.config_path().read_text())
    assert on_disk["models"]["opencode"]["fast"] == "openai/custom-fast"

    assert config.unset_user_model("opencode", "fast") is True
    # Reverts to the baked default.
    assert config.get_model("opencode", "fast") == "openai/gpt-5.3-codex-spark"
    # Unsetting again reports nothing was there.
    assert config.unset_user_model("opencode", "fast") is False


def test_unset_prunes_empty_provider_and_models():
    config.set_user_model("codex", "fast", "x")
    assert config.unset_user_model("codex", "fast") is True
    data = json.loads(config.config_path().read_text())
    assert data.get("models", {}) == {}


# --- provenance -------------------------------------------------------------


def test_describe_models_sources(monkeypatch):
    config.set_user_model("opencode", "strongest", "openai/mine")
    monkeypatch.setenv("ANTHROPIC_MODEL", "opus-env")
    rows = {(r["provider"], r["tier"]): r for r in config.describe_models()}

    assert rows[("opencode", "strongest")]["source"] == "user"
    assert rows[("opencode", "strongest")]["model"] == "openai/mine"
    assert rows[("claude_code", "strongest")]["source"] == "env:ANTHROPIC_MODEL"
    assert rows[("codex", "fast")]["source"] == "baked"
    # codex strongest is null in baked → still "baked", model None.
    assert rows[("codex", "strongest")]["source"] == "baked"
    assert rows[("codex", "strongest")]["model"] is None


# --- remote defaults --------------------------------------------------------


def test_remote_overrides_baked(monkeypatch):
    # Re-enable a (fake) remote and feed it through the fetch seam.
    monkeypatch.setenv("CAW_DEFAULTS_URL", "https://example.invalid/models.json")
    config._reset_remote_state()
    remote_payload = {"models": {"claude_code": {"strongest": "opus-remote"}}}
    monkeypatch.setattr(config, "_fetch_remote", lambda: remote_payload)

    config.refresh_defaults()
    assert config.get_model("claude_code", ModelTier.STRONGEST) == "opus-remote"
    # Untouched tiers still come from baked.
    assert config.get_model("claude_code", ModelTier.FAST) == "claude-haiku-4-5-20251001"

    rows = {(r["provider"], r["tier"]): r for r in config.describe_models()}
    assert rows[("claude_code", "strongest")]["source"] == "remote"


def test_disabled_remote_no_fetch(monkeypatch):
    # Default conftest sets CAW_DEFAULTS_URL=off; ensure _fetch_remote is never hit.
    called = {"n": 0}

    def _boom():
        called["n"] += 1
        return None

    monkeypatch.setattr(config, "_fetch_remote", _boom)
    config._reset_remote_state()
    assert config.get_model("claude_code", ModelTier.STRONGEST) == "opus"
    assert called["n"] == 0


# --- CLI --------------------------------------------------------------------


def test_cli_set_get_unset():
    res = runner.invoke(config_app, ["set", "claude", "strongest", "opus-cli"])
    assert res.exit_code == 0, res.output
    assert "claude_code strongest = opus-cli" in res.output

    res = runner.invoke(config_app, ["get", "claude_code", "strongest"])
    assert res.exit_code == 0
    assert res.output.strip() == "opus-cli"

    res = runner.invoke(config_app, ["unset", "claude_code", "strongest"])
    assert res.exit_code == 0
    assert "opus" in res.output  # reverts to baked "opus"


def test_cli_list_shows_providers():
    res = runner.invoke(config_app, ["list"])
    assert res.exit_code == 0
    assert "claude_code" in res.output
    assert "opencode" in res.output


def test_cli_rejects_unknown_tier():
    res = runner.invoke(config_app, ["set", "claude", "bogus", "x"])
    assert res.exit_code == 1
    assert "unknown tier" in res.output.lower()


def test_cli_path():
    res = runner.invoke(config_app, ["path"])
    assert res.exit_code == 0
    assert res.output.strip() == str(config.config_path())
