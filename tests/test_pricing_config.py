"""Tests for ~/.caw token pricing (caw.pricing) and the `caw pricing` CLI.

These rely on the autouse ``_isolate_caw_home`` fixture (in conftest) pointing
``CAW_HOME`` at a throwaway dir, so user overrides never touch the real config.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from caw import config, pricing
from caw.models import UsageStats
from caw.pricing_cli import app as pricing_app


runner = CliRunner()


def _usage(inp: int = 0, out: int = 0, cached: int = 0) -> UsageStats:
    return UsageStats(input_tokens=inp, output_tokens=out, cache_read_tokens=cached)


# --- baked-in pricing -------------------------------------------------------


def test_baked_pricing_resolution():
    assert pricing.get_pricing("opencode", "openai/gpt-5.5") == {
        "input": 5.0,
        "cached_input": 0.5,
        "output": 30.0,
    }
    assert pricing.get_pricing("codex", "gpt-5.3-codex") == {
        "input": 1.75,
        "cached_input": 0.175,
        "output": 14.0,
    }


def test_unknown_model_has_no_pricing():
    assert pricing.get_pricing("opencode", "openai/does-not-exist") == {}


def test_pro_model_omits_cached_input():
    # gpt-5.5-pro ships without a cached-input rate.
    rates = pricing.get_pricing("opencode", "openai/gpt-5.5-pro")
    assert rates == {"input": 30.0, "output": 180.0}


def test_compute_cost_from_baked():
    # 1M of each at gpt-5.5 rates: 5 + 0.5 + 30 = 35.5
    cost = pricing.compute_cost("opencode", "openai/gpt-5.5", _usage(1_000_000, 1_000_000, 1_000_000))
    assert cost == 35.5


def test_compute_cost_unknown_model_is_zero():
    assert pricing.compute_cost("opencode", "openai/unknown", _usage(1_000_000, 1_000_000)) == 0.0


# --- user overrides ---------------------------------------------------------


def test_user_override_merges_field_by_field():
    pricing.set_user_pricing("opencode", "openai/gpt-5.5", {"output": 99.0})
    rates = pricing.get_pricing("opencode", "openai/gpt-5.5")
    # output overridden, input/cached_input still baked.
    assert rates == {"input": 5.0, "cached_input": 0.5, "output": 99.0}


def test_user_override_adds_new_model():
    pricing.set_user_pricing("opencode", "openai/brand-new", {"input": 1.0, "output": 2.0})
    assert pricing.get_pricing("opencode", "openai/brand-new") == {"input": 1.0, "output": 2.0}


def test_compute_cost_reflects_user_override():
    pricing.set_user_pricing("opencode", "openai/gpt-5.5", {"output": 60.0})
    # 1M each: 5 + 0.5 + 60 = 65.5
    cost = pricing.compute_cost("opencode", "openai/gpt-5.5", _usage(1_000_000, 1_000_000, 1_000_000))
    assert cost == 65.5


def test_set_writes_file_and_unset_reverts():
    pricing.set_user_pricing("codex", "gpt-5.3-codex", {"output": 20.0})
    on_disk = json.loads(config.config_path().read_text())
    assert on_disk["pricing"]["codex"]["gpt-5.3-codex"] == {"output": 20.0}

    assert pricing.unset_user_pricing("codex", "gpt-5.3-codex") is True
    # Reverts to the baked rate.
    assert pricing.get_pricing("codex", "gpt-5.3-codex")["output"] == 14.0
    # Unsetting again reports nothing was there.
    assert pricing.unset_user_pricing("codex", "gpt-5.3-codex") is False


def test_unset_prunes_empty_agent_and_pricing():
    pricing.set_user_pricing("codex", "gpt-5.3-codex", {"output": 20.0})
    assert pricing.unset_user_pricing("codex", "gpt-5.3-codex") is True
    data = json.loads(config.config_path().read_text())
    assert data.get("pricing", {}) == {}


# --- provenance -------------------------------------------------------------


def test_describe_pricing_sources():
    pricing.set_user_pricing("opencode", "openai/gpt-5.5", {"output": 99.0})
    rows = {(r["agent"], r["model"]): r for r in pricing.describe_pricing()}

    assert rows[("opencode", "openai/gpt-5.5")]["source"] == "user"
    assert rows[("opencode", "openai/gpt-5.5")]["output"] == 99.0
    # An untouched model stays baked.
    assert rows[("codex", "gpt-5.3-codex")]["source"] == "baked"
    assert rows[("codex", "gpt-5.3-codex")]["output"] == 14.0


# --- CLI --------------------------------------------------------------------


def test_cli_set_get_unset():
    res = runner.invoke(pricing_app, ["set", "opencode", "openai/gpt-5.5", "--output", "77"])
    assert res.exit_code == 0, res.output
    assert "output=77" in res.output

    res = runner.invoke(pricing_app, ["get", "opencode", "openai/gpt-5.5"])
    assert res.exit_code == 0
    assert "output=77" in res.output
    assert "input=5" in res.output  # untouched field still shown from baked

    res = runner.invoke(pricing_app, ["unset", "opencode", "openai/gpt-5.5"])
    assert res.exit_code == 0
    assert "reverts to shipped" in res.output


def test_cli_set_requires_a_field():
    res = runner.invoke(pricing_app, ["set", "opencode", "openai/gpt-5.5"])
    assert res.exit_code == 1
    assert "at least one" in res.output.lower()


def test_cli_get_unknown_model_errors():
    res = runner.invoke(pricing_app, ["get", "opencode", "openai/nope"])
    assert res.exit_code == 1
    assert "no pricing" in res.output.lower()


def test_cli_list_shows_models():
    res = runner.invoke(pricing_app, ["list"])
    assert res.exit_code == 0
    assert "opencode" in res.output
    assert "gpt-5.5" in res.output


def test_cli_path():
    res = runner.invoke(pricing_app, ["path"])
    assert res.exit_code == 0
    assert res.output.strip() == str(config.config_path())
