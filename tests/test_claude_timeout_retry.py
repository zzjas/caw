"""Tests for how ClaudeCodeSession.send handles a turn the CLI reported as failed.

A failed request does not make the claude CLI exit non-zero.  It marks the
``result`` event ``is_error: true`` with an ``error_*`` subtype, and *may* also
report the failure as ordinary assistant text ("Request timed out", "API Error:
500 ...") or emit no output blocks at all — so a caller gets a successful Turn
back for work that never ran, and a harness that marks a task succeeded on a
returned trajectory marks it succeeded with nothing done.  The session retries
such a turn and raises when retries are exhausted.
"""

from __future__ import annotations

import pytest

from caw.models import TextBlock, ToolUse, Turn
from caw.providers.claude_code import (
    FAILED_TURN_RETRIES,
    ClaudeCodeSession,
    _turn_failed,
)


def _session() -> ClaudeCodeSession:
    return ClaudeCodeSession(mcp_servers=[])


def _turn(*blocks) -> Turn:
    return Turn(input="go", output=list(blocks))


def _failed(turn: Turn, *, is_error: bool = False, subtype: str = "success") -> bool:
    return _turn_failed(turn, is_error=is_error, subtype=subtype)


class TestResultEventSignal:
    """The primary signal: what the CLI's own result event says."""

    def test_the_error_flag_alone_is_a_failure(self):
        # No output at all — the shape a forced API failure actually produces.
        assert _failed(_turn(), is_error=True, subtype="error_during_execution")

    def test_an_error_subtype_alone_is_a_failure(self):
        assert _failed(_turn(TextBlock(text="partial")), subtype="error_during_execution")

    def test_a_clean_result_is_not_a_failure(self):
        assert not _failed(_turn(TextBlock(text="Done — committed v3.")))

    def test_an_empty_but_clean_turn_is_not_a_failure(self):
        assert not _failed(_turn())


class TestFailureTextFallback:
    """The fallback: the CLI reporting a failure only as assistant text."""

    def test_the_timeout_text_alone_is_a_failure(self):
        assert _failed(_turn(TextBlock(text="Request timed out")))

    def test_surrounding_whitespace_and_case_do_not_hide_it(self):
        assert _failed(_turn(TextBlock(text="  Request Timed Out.\n")))

    def test_the_longer_etimedout_wording_is_a_failure(self):
        assert _failed(_turn(TextBlock(text="Request timed out. Check your internet connection and proxy settings")))

    def test_an_api_error_is_a_failure(self):
        assert _failed(_turn(TextBlock(text="API Error: 500 Internal server error.")))

    def test_the_text_as_part_of_a_real_answer_is_not_a_failure(self):
        assert not _failed(_turn(TextBlock(text="The build failed because the request timed out upstream.")))


class TestRetry:
    def test_a_failed_turn_is_retried_and_the_good_turn_returned(self, monkeypatch):
        session = _session()
        attempts = []

        def fake_send_once(message: str) -> Turn:
            attempts.append(message)
            if len(attempts) == 1:
                session._last_is_error = True
                session._last_subtype = "error_during_execution"
                return _turn()
            session._last_is_error = False
            session._last_subtype = "success"
            return _turn(TextBlock(text="ok"))

        monkeypatch.setattr(session, "_send_once", fake_send_once)
        assert session.send("go").result == "ok"
        assert len(attempts) == 2

    def test_exhausted_retries_raise_instead_of_returning_a_fake_success(self, monkeypatch):
        session = _session()
        attempts = []

        def fake_send_once(message: str) -> Turn:
            attempts.append(message)
            session._last_is_error = True
            session._last_subtype = "error_during_execution"
            return _turn()

        monkeypatch.setattr(session, "_send_once", fake_send_once)
        with pytest.raises(RuntimeError, match="failed turn"):
            session.send("go")
        assert len(attempts) == FAILED_TURN_RETRIES + 1

    def test_a_normal_turn_is_sent_once(self, monkeypatch):
        session = _session()
        attempts = []

        def fake_send_once(message: str) -> Turn:
            attempts.append(message)
            session._last_subtype = "success"
            return _turn(TextBlock(text="ok"))

        monkeypatch.setattr(session, "_send_once", fake_send_once)
        session.send("go")
        assert len(attempts) == 1

    def test_a_turn_that_did_work_first_is_kept_not_retried(self, monkeypatch):
        # Its tool calls are real; re-sending would redo them.
        session = _session()
        attempts = []

        def fake_send_once(message: str) -> Turn:
            attempts.append(message)
            session._last_is_error = True
            session._last_subtype = "error_during_execution"
            return _turn(ToolUse(id="1", name="Bash"), TextBlock(text="Request timed out"))

        monkeypatch.setattr(session, "_send_once", fake_send_once)
        turn = session.send("go")
        assert len(attempts) == 1
        assert turn.tool_calls

    def test_a_usage_limit_is_returned_for_the_autowait_loop_not_retried(self, monkeypatch):
        # Even flagged as an error, a limit must reach the caller as a turn so
        # Session._send_with_autowait can sleep and resume.
        session = _session()
        attempts = []

        def fake_send_once(message: str) -> Turn:
            attempts.append(message)
            session._last_is_error = True
            session._last_subtype = "error_during_execution"
            return _turn(TextBlock(text="You've hit your session limit · resets 7:10pm (America/Chicago)"))

        monkeypatch.setattr(session, "_send_once", fake_send_once)
        turn = session.send("go")
        assert len(attempts) == 1
        assert session.detect_usage_limit(turn) is not None


class TestLoggerSurface:
    def test_the_retry_path_uses_the_logger_protocol(self, monkeypatch):
        """The retry warning must use warn(), the method AgentLogger defines.

        A logger implementing exactly the protocol (info/warn/error, e.g. the
        RedisLogger this runs under) would otherwise raise AttributeError on the
        one path this code exists to handle.
        """

        class ProtocolLogger:
            def __init__(self):
                self.warnings = []

            def info(self, message: str) -> None: ...
            def warn(self, message: str) -> None:
                self.warnings.append(message)

            def error(self, message: str) -> None: ...

        session = _session()
        logger = ProtocolLogger()
        session.set_logger(logger)

        def fake_send_once(message: str) -> Turn:
            session._last_is_error = True
            session._last_subtype = "error_during_execution"
            return _turn()

        monkeypatch.setattr(session, "_send_once", fake_send_once)
        with pytest.raises(RuntimeError):
            session.send("go")
        assert len(logger.warnings) == FAILED_TURN_RETRIES + 1
        assert "error_during_execution" in logger.warnings[0]
