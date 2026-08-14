"""Tests for session options that used to be accepted and then dropped.

``Agent(..., cwd=..., extra_config=...)`` type-checked, ran, and did nothing:
provider sessions pick the options they know out of ``**kwargs`` by name, so
anything they did not know about vanished without a word. A caller asking for
a sandboxed working directory got an agent running in the *server's* directory
and no indication of it.

These cover the three halves of the fix: the options are honored, an unknown
one is reported instead of swallowed, and a bad ``cwd`` fails as itself rather
than as "the CLI is not installed".
"""

from __future__ import annotations

import logging

import pytest

from caw.providers.claude_code import ClaudeCodeProvider
from caw.providers.claudep import ClaudePProvider
from caw.providers.codex import CodexProvider, CodexSession, _build_extra_config_args
from caw.providers.opencode import OpencodeProvider

PROVIDERS = [CodexProvider(), ClaudeCodeProvider(), ClaudePProvider(), OpencodeProvider()]


class _FakeProc:
    """Just enough of Popen for one turn that produces no events."""

    returncode = 0

    def __init__(self):
        self.stdout = iter(())
        self.stderr = type("_E", (), {"read": staticmethod(lambda: "")})()

    def wait(self):
        return 0

    def kill(self):
        pass


class TestCwd:
    @pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
    def test_cwd_reaches_the_session(self, provider, tmp_path):
        session = provider.start_session(mcp_servers=[], cwd=str(tmp_path))
        assert session._cwd == str(tmp_path)

    @pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
    def test_cwd_survives_resume(self, provider, tmp_path):
        session = provider.resume_session(
            mcp_servers=[], session_id="s", resume_key="r", cwd=str(tmp_path)
        )
        assert session._cwd == str(tmp_path)

    @pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
    def test_missing_cwd_is_reported_as_itself(self, provider, tmp_path):
        # Not as "codex CLI not found" — Popen raises FileNotFoundError for a
        # missing cwd too, and every provider maps that to an install hint.
        missing = tmp_path / "nope"
        with pytest.raises(NotADirectoryError, match="nope"):
            provider.start_session(mcp_servers=[], cwd=str(missing))

    @pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
    def test_no_cwd_means_inherit(self, provider):
        assert provider.start_session(mcp_servers=[])._cwd is None

    def test_cwd_is_passed_to_the_subprocess(self, tmp_path, monkeypatch):
        seen = {}

        def fake_popen(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["cwd"] = kwargs.get("cwd")
            return _FakeProc()

        monkeypatch.setattr("caw.providers.codex.subprocess.Popen", fake_popen)
        CodexProvider().start_session(mcp_servers=[], cwd=str(tmp_path)).send("hi")
        assert seen["cwd"] == str(tmp_path)


class TestExtraConfig:
    def test_values_are_encoded_as_toml(self):
        args = _build_extra_config_args(
            {
                "sandbox_workspace_write.exclude_slash_tmp": True,
                "model_reasoning_effort": "high",
                "some.number": 3,
                "some.list": ["a", "b"],
            }
        )
        assert args == [
            "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
            "-c", 'model_reasoning_effort="high"',
            "-c", "some.number=3",
            "-c", 'some.list=["a", "b"]',
        ]

    def test_empty_config_adds_nothing(self):
        assert _build_extra_config_args({}) == []

    def test_flags_land_on_the_command_before_the_prompt(self, monkeypatch):
        seen = {}

        def fake_popen(cmd, **kwargs):
            seen["cmd"] = cmd
            return _FakeProc()

        monkeypatch.setattr("caw.providers.codex.subprocess.Popen", fake_popen)
        session = CodexProvider().start_session(
            mcp_servers=[],
            sandbox="workspace-write",
            extra_config={"sandbox_workspace_write.exclude_slash_tmp": True},
        )
        session.send("draw me a sticker")

        cmd = seen["cmd"]
        assert "-c" in cmd
        flag = cmd.index("sandbox_workspace_write.exclude_slash_tmp=true")
        # codex only reads -c before the positional prompt, which is last.
        assert cmd[-1] == "draw me a sticker"
        assert flag < len(cmd) - 1

    def test_session_keeps_its_own_copy(self):
        config = {"a.b": 1}
        session = CodexSession(mcp_servers=[], extra_config=config)
        config["a.b"] = 2
        assert session._extra_config == {"a.b": 1}


class TestUnknownOptions:
    @pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
    def test_unknown_option_warns(self, provider, caplog):
        with caplog.at_level(logging.WARNING, logger="caw.provider"):
            provider.start_session(mcp_servers=[], no_such_option=1)
        assert "no_such_option" in caplog.text

    @pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.name)
    def test_common_options_are_quiet(self, provider, caplog):
        with caplog.at_level(logging.WARNING, logger="caw.provider"):
            provider.start_session(
                mcp_servers=[],
                model="m",
                system_prompt="s",
                session_id="i",
                reasoning="high",
                cwd=None,
            )
        assert caplog.text == ""

    def test_provider_specific_options_are_quiet(self, caplog):
        # Each of these is injected by resolve_tool_restrictions or is that
        # provider's own passthrough, so none of them is a mistake.
        cases = [
            (CodexProvider(), {"sandbox": "read-only", "extra_config": {"a": 1}}),
            (ClaudeCodeProvider(), {"disallowed_tools": ["Bash"]}),
            (ClaudePProvider(), {"disallowed_tools": ["Bash"], "timeout_sec": 30.0,
                                 "strip_provider_env": False}),
            (OpencodeProvider(), {"disabled_tools": ["bash"]}),
        ]
        for provider, kwargs in cases:
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="caw.provider"):
                provider.start_session(mcp_servers=[], **kwargs)
            assert caplog.text == "", f"{provider.name} warned about {kwargs}"

    def test_extra_config_warns_on_providers_that_cannot_use_it(self, caplog):
        # It is codex-only, and silently dropping it is the bug this fixes.
        for provider in (ClaudeCodeProvider(), ClaudePProvider(), OpencodeProvider()):
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="caw.provider"):
                provider.start_session(mcp_servers=[], extra_config={"a": 1})
            assert "extra_config" in caplog.text


class TestSandboxFlags:
    """`codex exec` dropped `--full-auto`; emitting it exits 2 before any work."""

    def _cmd(self, monkeypatch, **kwargs):
        seen = {}

        def fake_popen(cmd, **kw):
            seen["cmd"] = cmd
            return _FakeProc()

        monkeypatch.setattr("caw.providers.codex.subprocess.Popen", fake_popen)
        CodexProvider().start_session(mcp_servers=[], **kwargs).send("hi")
        return seen["cmd"]

    def test_restrictive_sandbox_does_not_pass_full_auto(self, monkeypatch):
        cmd = self._cmd(monkeypatch, sandbox="workspace-write")
        assert "--full-auto" not in cmd
        assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"

    def test_read_only_sandbox_does_not_pass_full_auto(self, monkeypatch):
        cmd = self._cmd(monkeypatch, sandbox="read-only")
        assert "--full-auto" not in cmd
        assert cmd[cmd.index("--sandbox") + 1] == "read-only"

    def test_no_sandbox_still_bypasses(self, monkeypatch):
        cmd = self._cmd(monkeypatch)
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--sandbox" not in cmd

    def test_danger_full_access_bypasses(self, monkeypatch):
        cmd = self._cmd(monkeypatch, sandbox="danger-full-access")
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--sandbox" not in cmd
