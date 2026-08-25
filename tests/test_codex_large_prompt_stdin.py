"""Large prompts must go through stdin to avoid Linux ARG_MAX / E2BIG."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from caw.providers.codex import (
    CodexSession,
    _PROMPT_STDIN_THRESHOLD_BYTES,
    _prompt_exceeds_argv_budget,
)


class TestPromptArgvBudget:
    def test_small_prompt_fits_on_argv(self):
        assert _prompt_exceeds_argv_budget(["codex", "exec"], "hello") is False

    def test_large_prompt_requires_stdin(self):
        prompt = "x" * (_PROMPT_STDIN_THRESHOLD_BYTES + 1)
        assert _prompt_exceeds_argv_budget(["codex", "exec"], prompt) is True

    def test_custom_threshold(self):
        assert _prompt_exceeds_argv_budget(["codex"], "abcd", threshold=3) is True
        assert _prompt_exceeds_argv_budget(["codex"], "ab", threshold=100) is False


class TestCodexSessionSendStdin:
    def test_send_pipes_large_prompt_via_stdin(self):
        session = CodexSession(mcp_servers=[])
        large = "P" * (_PROMPT_STDIN_THRESHOLD_BYTES + 50)

        stdout = io.StringIO(
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"output_tokens":1}}\n'
        )
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = stdout
        proc.stderr = io.StringIO("")
        proc.returncode = 0

        with patch("caw.providers.codex.subprocess.Popen", return_value=proc) as popen:
            turn = session.send(large)

        cmd = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        assert cmd[-1] == "-"
        assert kwargs["stdin"] is not None  # PIPE, not DEVNULL
        proc.stdin.write.assert_called_once_with(large)
        proc.stdin.close.assert_called_once()
        assert "ok" in turn.result

    def test_send_keeps_small_prompt_on_argv(self):
        from subprocess import DEVNULL

        session = CodexSession(mcp_servers=[])
        small = "short prompt"

        stdout = io.StringIO(
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"output_tokens":1}}\n'
        )
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = stdout
        proc.stderr = io.StringIO("")
        proc.returncode = 0

        with patch("caw.providers.codex.subprocess.Popen", return_value=proc) as popen:
            session.send(small)

        cmd = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        assert cmd[-1] == small
        assert kwargs["stdin"] is DEVNULL
        proc.stdin.write.assert_not_called()
        proc.stdin.close.assert_not_called()
