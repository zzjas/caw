"""Tests for auto-provider selection and graceful first-send fallback."""

from __future__ import annotations

import shutil

import pytest

from caw import Agent
from caw.agent import _PROVIDER_REGISTRY, get_provider_order, register_provider, set_provider_order
from caw.models import TextBlock, Trajectory, Turn, UsageStats
from caw.provider import Provider, ProviderSession


# ---------------------------------------------------------------------------
# Scriptable provider/session — behavior drives what send() does.
# ---------------------------------------------------------------------------


class _ScriptedSession(ProviderSession):
    def __init__(self, name: str, behavior: str):
        self._name = name
        self._behavior = behavior  # "ok" | "raise" | "limit" | "raise_second"
        self._sends = 0
        self._turns: list[Turn] = []

    def send(self, message: str) -> Turn:
        self._sends += 1
        if self._behavior == "raise":
            raise RuntimeError(f"{self._name} boom")
        if self._behavior == "raise_second" and self._sends >= 2:
            raise RuntimeError(f"{self._name} boom2")
        turn = Turn(input=message, output=[TextBlock(text=f"{self._name}:{message}")], usage=UsageStats())
        self._turns.append(turn)
        return turn

    def end(self) -> Trajectory:
        return self.trajectory

    def detect_usage_limit(self, turn: Turn) -> int | None:
        return 1 if self._behavior == "limit" else None

    @property
    def last_raw_output(self) -> str:
        return "{}"

    @property
    def trajectory(self) -> Trajectory:
        return Trajectory(agent=self._name, turns=list(self._turns), usage=UsageStats())


def _make_provider(prov_name: str, binary: str, behavior: str) -> type[Provider]:
    class _P(Provider):
        @property
        def name(self) -> str:
            return prov_name

        @property
        def binary_name(self) -> str:
            return binary

        def resolve_tool_restrictions(self, tools):
            return {}

        def start_session(self, mcp_servers, **kwargs) -> _ScriptedSession:
            return _ScriptedSession(prov_name, behavior)

    return _P


@pytest.fixture
def reg():
    """Register scripted providers; clean them out of the global registry after."""
    names: list[str] = []

    def _register(prov_name: str, binary: str, behavior: str) -> str:
        register_provider(prov_name, _make_provider(prov_name, binary, behavior))
        names.append(prov_name)
        return prov_name

    yield _register
    for n in names:
        _PROVIDER_REGISTRY.pop(n, None)


@pytest.fixture
def installed(monkeypatch):
    """Control which binaries 'exist' on PATH for find_binary()."""
    present: set[str] = set()
    monkeypatch.setattr(shutil, "which", lambda b: f"/usr/bin/{b}" if b in present else None)
    return present


@pytest.fixture(autouse=True)
def _reset_global_order():
    yield
    set_provider_order(None)


def _send_once(agent: Agent, message: str = "hi") -> Turn:
    session = agent.start_session()
    turn = session.send(message)
    session.end()
    return turn


# ---------------------------------------------------------------------------
# Selection (no ping) — first installed wins
# ---------------------------------------------------------------------------


def test_selects_first_installed(reg, installed):
    reg("pa", "abin", "ok")
    reg("pb", "bbin", "ok")
    reg("pc", "cbin", "ok")
    installed.update({"bbin", "cbin"})  # pa NOT installed

    turn = _send_once(Agent(provider=["pa", "pb", "pc"]))
    assert turn.result.startswith("pb:")  # skipped pa, picked first installed


def test_provider_property_picks_first_installed(reg, installed):
    reg("pa", "abin", "ok")
    reg("pb", "bbin", "ok")
    installed.add("bbin")
    assert Agent(provider=["pa", "pb"]).provider.name == "pb"


# ---------------------------------------------------------------------------
# First-send catch — no exceptions surface to the caller
# ---------------------------------------------------------------------------


def test_first_send_falls_back_on_exception(reg, installed):
    reg("pa", "abin", "raise")
    reg("pb", "bbin", "ok")
    installed.update({"abin", "bbin"})

    turn = _send_once(Agent(provider=["pa", "pb"]))
    assert turn.result.startswith("pb:")  # pa raised, transparently moved on


def test_first_send_falls_back_on_rate_limit(reg, installed):
    reg("pa", "abin", "limit")
    reg("pb", "bbin", "ok")
    installed.update({"abin", "bbin"})

    turn = _send_once(Agent(provider=["pa", "pb"]))
    assert turn.result.startswith("pb:")  # pa rate-limited, moved on instead of waiting


def test_all_providers_exhausted_raises(reg, installed):
    reg("pa", "abin", "raise")
    reg("pb", "bbin", "raise")
    installed.update({"abin", "bbin"})

    with pytest.raises(RuntimeError, match="boom"):
        _send_once(Agent(provider=["pa", "pb"]))


def test_commit_after_first_send_no_further_fallback(reg, installed):
    # pa succeeds on the first send (committing the session) then raises on the
    # second — that must propagate, not silently jump to pb.
    reg("pa", "abin", "raise_second")
    reg("pb", "bbin", "ok")
    installed.update({"abin", "bbin"})

    session = Agent(provider=["pa", "pb"]).start_session()
    first = session.send("one")
    assert first.result.startswith("pa:")
    with pytest.raises(RuntimeError, match="boom2"):
        session.send("two")
    session.end()


# ---------------------------------------------------------------------------
# Pinned single provider — unchanged behavior (no fallback)
# ---------------------------------------------------------------------------


def test_pinned_single_provider_does_not_fall_back(reg, installed):
    reg("pa", "abin", "raise")
    reg("pb", "bbin", "ok")
    installed.update({"abin", "bbin"})

    with pytest.raises(RuntimeError, match="boom"):
        _send_once(Agent(provider="pa"))  # pinned → propagates, never tries pb


# ---------------------------------------------------------------------------
# Order sources: global setting, env var, "auto"
# ---------------------------------------------------------------------------


def test_global_order_with_auto(reg, installed):
    reg("pa", "abin", "ok")
    reg("pb", "bbin", "ok")
    installed.add("bbin")  # pa not installed
    set_provider_order(["pa", "pb"])
    assert get_provider_order() == ["pa", "pb"]

    turn = _send_once(Agent(provider="auto"))
    assert turn.result.startswith("pb:")


def test_env_comma_list(reg, installed, monkeypatch):
    reg("pa", "abin", "ok")
    reg("pb", "bbin", "ok")
    installed.add("bbin")
    monkeypatch.setenv("CAW_PROVIDER", "pa,pb")

    turn = _send_once(Agent())  # no explicit provider → env order
    assert turn.result.startswith("pb:")


def test_explicit_list_overrides_global(reg, installed):
    reg("pa", "abin", "ok")
    reg("pb", "bbin", "ok")
    installed.update({"abin", "bbin"})
    set_provider_order(["pb"])  # global says pb

    # Explicit list on the Agent wins.
    turn = _send_once(Agent(provider=["pa", "pb"]))
    assert turn.result.startswith("pa:")


def test_unknown_provider_in_order_raises(reg):
    with pytest.raises(ValueError, match="Unknown provider 'nope'"):
        Agent(provider=["nope"]).start_session()
