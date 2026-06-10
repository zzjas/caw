"""Tests for interactive provider selection (`Agent.interactive(select_provider=True)`)
and the supporting `installed_providers()` helper + arrow-key menu."""

from __future__ import annotations

import io
import os
import select
import shutil
import sys
import termios
import tty

import pytest

from caw import Agent, installed_providers
from caw.agent import _PROVIDER_REGISTRY, register_provider
from caw.models import InteractiveResult
from caw.provider import Provider


# ---------------------------------------------------------------------------
# Scriptable providers — each records the prompt it was launched with.
# ---------------------------------------------------------------------------


def _make_provider(prov_name: str, binary: str):
    class _P(Provider):
        @property
        def name(self):
            return prov_name

        @property
        def binary_name(self):
            return binary

        def check_auth(self):
            return None

        def resolve_model(self, tier):
            return None

        def resolve_tool_restrictions(self, tools):
            return {}

        def start_session(self, mcp_servers, **kwargs):
            raise NotImplementedError

        def start_interactive(self, initial_prompt, mcp_servers, capture_bytes=0, **kwargs):
            return InteractiveResult(exit_code=0, output=f"launched:{prov_name}")

    return _P


@pytest.fixture
def reg():
    """Isolate the provider registry to only the scripted providers a test
    registers (real providers like opencode bypass ``shutil.which``, so they'd
    otherwise leak into ``installed_providers()``).  Restores it afterwards."""
    saved = dict(_PROVIDER_REGISTRY)
    _PROVIDER_REGISTRY.clear()

    def _register(prov_name: str, binary: str) -> str:
        register_provider(prov_name, _make_provider(prov_name, binary))
        return prov_name

    yield _register
    _PROVIDER_REGISTRY.clear()
    _PROVIDER_REGISTRY.update(saved)


@pytest.fixture
def installed(monkeypatch):
    """Control which binaries 'exist' on PATH for find_binary()."""
    present: set[str] = set()
    monkeypatch.setattr(shutil, "which", lambda b: f"/usr/bin/{b}" if b in present else None)
    return present


# ---------------------------------------------------------------------------
# installed_providers()
# ---------------------------------------------------------------------------


def test_installed_providers_filters_by_binary(reg, installed):
    reg("pa", "bin_a")
    reg("pb", "bin_b")
    installed.update({"bin_a"})  # only pa installed

    names = [name for name, _ in installed_providers()]
    assert "pa" in names
    assert "pb" not in names


def test_installed_providers_dedupes_aliases(reg, installed):
    cls = _make_provider("dup", "bin_dup")
    register_provider("dup1", cls)
    register_provider("dup2", cls)  # same class under a second alias
    try:
        installed.update({"bin_dup"})
        names = [name for name, _ in installed_providers()]
        # Deduped across aliases -> only the first registration shows up.
        assert names.count("dup1") == 1
        assert "dup2" not in names
    finally:
        _PROVIDER_REGISTRY.pop("dup1", None)
        _PROVIDER_REGISTRY.pop("dup2", None)


# ---------------------------------------------------------------------------
# Agent.interactive(select_provider=...)
# ---------------------------------------------------------------------------


def test_interactive_launches_menu_choice_not_configured_provider(reg, installed, monkeypatch):
    reg("pa", "bin_a")
    reg("pb", "bin_b")
    installed.update({"bin_a", "bin_b"})

    # User picks index 1 (pb) from the menu.
    monkeypatch.setattr("caw._menu.select_from_menu", lambda title, options, **kw: 1)

    agent = Agent(provider="pa")  # configured to pa...
    result = agent.interactive("hello", select_provider=True)
    assert result.output == "launched:pb"  # ...but launched the menu choice pb
    assert result.exit_code == 0


def test_interactive_cancel_returns_130(reg, installed, monkeypatch):
    reg("pa", "bin_a")
    installed.update({"bin_a"})
    monkeypatch.setattr("caw._menu.select_from_menu", lambda title, options, **kw: None)

    agent = Agent(provider="pa")
    result = agent.interactive("hello", select_provider=True)
    assert result.exit_code == 130
    assert result.output == ""


def test_interactive_no_installed_providers_raises(reg, installed, monkeypatch):
    reg("pa", "bin_a")  # registered but nothing in `installed` set -> not on PATH
    monkeypatch.setattr("caw._menu.select_from_menu", lambda *a, **k: 0)

    agent = Agent(provider="pa")
    with pytest.raises(RuntimeError, match="No coding-agent CLIs are installed"):
        agent.interactive("hello", select_provider=True)


# ---------------------------------------------------------------------------
# Arrow-key menu decoding (terminal I/O stubbed)
# ---------------------------------------------------------------------------


class _FakeTty(io.StringIO):
    def isatty(self):
        return True

    def fileno(self):
        return 0


def _drive_menu(keys: bytes, monkeypatch, *, default: int = 0, options=("a", "b", "c")):
    """Run select_from_menu against a scripted byte stream, no real terminal."""
    from caw._menu import select_from_menu

    buf = bytearray(keys)

    def fake_read(fd, n):
        if not buf:
            return b""
        chunk = bytes(buf[:n])
        del buf[:n]
        return chunk

    # Bytes still buffered -> "readable"; empty -> not (so a trailing bare ESC cancels).
    monkeypatch.setattr(os, "read", fake_read)
    monkeypatch.setattr(select, "select", lambda r, w, x, t: ((r if buf else []), [], []))
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: None)
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, attrs: None)
    monkeypatch.setattr(tty, "setraw", lambda fd: None)
    monkeypatch.setattr(sys, "stdin", _FakeTty())
    monkeypatch.setattr(sys, "stdout", _FakeTty())
    return select_from_menu("Pick:", list(options), default=default)


@pytest.mark.parametrize(
    "keys, default, expected",
    [
        (b"\x1b[B\x1b[B\r", 0, 2),  # down, down, enter
        (b"\x1b[B\x1b[A\r", 0, 0),  # down, up, enter
        (b"j\r", 0, 1),  # vim-down, enter
        (b"k\r", 0, 2),  # vim-up wraps to last, enter
        (b"\r", 1, 1),  # enter on the default
        (b"q", 0, None),  # q cancels
        (b"\x1b", 0, None),  # bare ESC cancels
        (b"\x1b[C\r", 0, 0),  # right-arrow ignored, enter
    ],
)
def test_menu_key_decoding(keys, default, expected, monkeypatch):
    assert _drive_menu(keys, monkeypatch, default=default) == expected


def test_menu_ctrl_c_raises(monkeypatch):
    with pytest.raises(KeyboardInterrupt):
        _drive_menu(b"\x03", monkeypatch)


def test_menu_requires_tty(monkeypatch):
    from caw._menu import select_from_menu

    monkeypatch.setattr(sys, "stdin", io.StringIO())  # isatty() -> False
    with pytest.raises(RuntimeError, match="interactive terminal"):
        select_from_menu("Pick:", ["a", "b"])
