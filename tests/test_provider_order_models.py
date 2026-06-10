"""Tests for per-provider model selection attached to set_provider_order."""

from __future__ import annotations

import shutil

import pytest

from caw import Agent
from caw.agent import (
    _PROVIDER_REGISTRY,
    get_provider_models,
    register_provider,
    set_provider_order,
)
from caw.models import ModelTier, TextBlock, Trajectory, Turn, UsageStats
from caw.provider import Provider, ProviderSession


class _CaptureSession(ProviderSession):
    def __init__(self, name: str, model, behavior: str = "ok"):
        self._name = name
        self.model = model  # what the provider was asked to use
        self._behavior = behavior
        self._turns: list[Turn] = []

    def send(self, message: str) -> Turn:
        if self._behavior == "raise":
            raise RuntimeError(f"{self._name} boom")
        turn = Turn(input=message, output=[TextBlock(text=f"{self._name}:{message}")], usage=UsageStats())
        self._turns.append(turn)
        return turn

    def end(self) -> Trajectory:
        return self.trajectory

    def detect_usage_limit(self, turn: Turn) -> int | None:
        return None

    @property
    def last_raw_output(self) -> str:
        return "{}"

    @property
    def trajectory(self) -> Trajectory:
        return Trajectory(agent=self._name, turns=list(self._turns), usage=UsageStats())


# Records the model each provider's session was built with.
_CAPTURED: dict[str, object] = {}


def _make_provider(prov_name: str, binary: str, behavior: str = "ok"):
    class _P(Provider):
        @property
        def name(self) -> str:
            return prov_name

        @property
        def binary_name(self) -> str:
            return binary

        def resolve_tool_restrictions(self, tools):
            return {}

        def resolve_model(self, tier: ModelTier) -> str:
            # Deterministic tier→string so ModelTier flow is observable.
            return f"{prov_name}-{tier.value}"

        def start_session(self, mcp_servers, **kwargs) -> _CaptureSession:
            model = kwargs.get("model")
            _CAPTURED[prov_name] = model
            return _CaptureSession(prov_name, model, behavior)

    return _P


@pytest.fixture
def reg():
    names: list[str] = []

    def _register(prov_name: str, binary: str, behavior: str = "ok") -> str:
        register_provider(prov_name, _make_provider(prov_name, binary, behavior))
        names.append(prov_name)
        return prov_name

    yield _register
    for n in names:
        _PROVIDER_REGISTRY.pop(n, None)


@pytest.fixture
def installed(monkeypatch):
    present: set[str] = set()
    monkeypatch.setattr(shutil, "which", lambda b: f"/usr/bin/{b}" if b in present else None)
    return present


@pytest.fixture(autouse=True)
def _reset_global_order():
    _CAPTURED.clear()
    yield
    set_provider_order(None)
    _CAPTURED.clear()


# --- set_provider_order parsing ---------------------------------------------


def test_tuple_entries_record_models():
    set_provider_order([("pa", "model-a"), "pb", ("pc", ModelTier.STRONGEST)])
    assert get_provider_models() == {"pa": "model-a", "pc": ModelTier.STRONGEST}


def test_models_kwarg_merges():
    set_provider_order(["pa", "pb"], models={"pb": "model-b"})
    assert get_provider_models() == {"pb": "model-b"}


def test_clearing_order_clears_models():
    set_provider_order([("pa", "m")])
    set_provider_order(None)
    assert get_provider_models() == {}


# --- model flows into the session ------------------------------------------


def test_order_model_string_applied(reg, installed):
    reg("pa", "abin")
    installed.add("abin")
    set_provider_order([("pa", "concrete-x")])

    agent = Agent(provider="auto")
    agent.start_session()
    assert _CAPTURED["pa"] == "concrete-x"


def test_order_model_tier_resolved_per_provider(reg, installed):
    reg("pa", "abin")
    installed.add("abin")
    set_provider_order([("pa", ModelTier.STRONGEST)])

    Agent(provider="auto").start_session()
    assert _CAPTURED["pa"] == "pa-strongest"  # resolved via provider.resolve_model


def test_explicit_agent_model_beats_order_model(reg, installed):
    reg("pa", "abin")
    installed.add("abin")
    set_provider_order([("pa", "order-model")])

    Agent(provider="auto", model="explicit").start_session()
    assert _CAPTURED["pa"] == "explicit"


def test_order_model_survives_fallback(reg, installed):
    # pa is the first choice but fails on first send; pb is reached as a
    # fallback and must still get its own order-model (a bare agent-level model
    # string would be dropped on fallback, but a per-provider one is not).
    reg("pa", "abin", behavior="raise")
    reg("pb", "bbin")
    installed.update({"abin", "bbin"})
    set_provider_order([("pa", "model-a"), ("pb", "model-b")])

    session = Agent(provider="auto").start_session()
    turn = session.send("hi")
    assert turn.result.startswith("pb:")
    assert _CAPTURED["pb"] == "model-b"
