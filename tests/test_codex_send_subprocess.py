"""Tests for CodexSession.send subprocess handling.

Covers the failure-surfacing path (nonzero exit + plain-text ``Error:`` stdout
lines must raise instead of returning an empty turn), the stdin prompt
delivery, the usage-limit exemption, and the fresh-vs-resume sandbox flag
mapping. All tests run against a fake ``codex`` executable placed on PATH.
"""

from __future__ import annotations

import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

from caw.providers.codex import CodexSession

FAKE_CODEX = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys

    argv_log = os.environ.get("FAKE_CODEX_ARGV")
    if argv_log:
        with open(argv_log, "a") as f:
            f.write(json.dumps(sys.argv[1:]) + "\\n")

    prompt = sys.stdin.read() if sys.argv[-1] == "-" else ""

    mode = os.environ.get("FAKE_CODEX_MODE", "ok")
    print(json.dumps({"type": "thread.started", "thread_id": "thread-123"}))
    if mode == "error":
        print("Error: turn/start failed: Input exceeds the maximum length of 1048576 characters. (code -32602)")
        sys.exit(1)
    if mode == "limit":
        print(json.dumps({"type": "turn.failed", "message": "You've hit your usage limit. try again at 3:47 PM"}))
        sys.exit(1)
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "echo:" + prompt}}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}}))
    """
)


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch) -> Path:
    binary = tmp_path / "codex"
    binary.write_text(FAKE_CODEX)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_CODEX_ARGV", str(argv_log))
    return argv_log


def _argv_calls(argv_log: Path) -> list[list[str]]:
    return [json.loads(line) for line in argv_log.read_text().splitlines()]


class TestFailureSurfacing:
    def test_nonzero_exit_with_plain_error_line_raises(self, fake_codex, monkeypatch):
        """A plain-text ``Error:`` stdout line after valid JSONL must not be swallowed."""
        monkeypatch.setenv("FAKE_CODEX_MODE", "error")
        session = CodexSession(mcp_servers=[])
        with pytest.raises(RuntimeError, match="Input exceeds the maximum length"):
            session.send("hello")

    def test_usage_limit_turn_does_not_raise(self, fake_codex, monkeypatch):
        """Usage-limit failures surface as TextBlocks so the auto-wait loop can retry."""
        monkeypatch.setenv("FAKE_CODEX_MODE", "limit")
        session = CodexSession(mcp_servers=[])
        turn = session.send("hello")
        assert "usage limit" in turn.result.lower()


class TestPromptDelivery:
    def test_prompt_arrives_via_stdin(self, fake_codex):
        session = CodexSession(mcp_servers=[])
        turn = session.send("marker-prompt-42")
        assert "echo:marker-prompt-42" in turn.result
        (argv,) = _argv_calls(fake_codex)
        assert argv[-1] == "-"


class TestSandboxFlags:
    def test_fresh_turn_uses_sandbox_flag_and_resume_uses_config_override(self, fake_codex):
        session = CodexSession(mcp_servers=[], sandbox="read-only")
        session.send("first")
        session.send("second")
        fresh, resume = _argv_calls(fake_codex)
        assert ["--sandbox", "read-only"] == fresh[1:3]
        assert "resume" not in fresh
        assert ["resume", "thread-123"] == resume[1:3]
        assert "--sandbox" not in resume
        assert 'sandbox_mode="read-only"' in resume
