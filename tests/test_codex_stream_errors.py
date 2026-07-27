"""Tests for how CodexSession._process_event handles error events.

Codex emits transient stream-retry notices ("Reconnecting... 2/5 ...") as
`error` events while it reconnects internally; these must not abort the turn.
Terminal errors and turn.failed must still raise.
"""

from __future__ import annotations

import pytest

from caw.providers.codex import CodexSession


def _session() -> CodexSession:
    return CodexSession(mcp_servers=[])


def _process(session: CodexSession, event: dict):
    return session._process_event(event, blocks=[], tool_blocks={}, display=None)


class TestTransientStreamErrors:
    def test_reconnecting_error_does_not_raise(self):
        event = {
            "type": "error",
            "message": (
                "Reconnecting... 2/5 (stream disconnected before completion: "
                "websocket closed by server before response.completed)"
            ),
        }
        assert _process(_session(), event) is None

    def test_reconnecting_first_attempt_does_not_raise(self):
        event = {"type": "error", "message": "Reconnecting... 1/5 (stream error)"}
        assert _process(_session(), event) is None

    def test_terminal_error_still_raises(self):
        event = {"type": "error", "message": "something went irrecoverably wrong"}
        with pytest.raises(RuntimeError, match="Codex turn failed"):
            _process(_session(), event)

    def test_turn_failed_with_reconnecting_message_still_raises(self):
        # turn.failed is terminal even if the message mentions reconnecting —
        # retries are exhausted at that point.
        event = {"type": "turn.failed", "message": "Reconnecting... 5/5 (gave up)"}
        with pytest.raises(RuntimeError, match="Codex turn failed"):
            _process(_session(), event)

    def test_reconnecting_mentioned_mid_message_still_raises(self):
        # Only messages that ARE retry notices (prefix match) are transient.
        event = {"type": "error", "message": "fatal: gave up Reconnecting... 5/5"}
        with pytest.raises(RuntimeError, match="Codex turn failed"):
            _process(_session(), event)

    def test_the_retry_notice_logs_through_the_logger_protocol(self):
        """The notice must use warn(), the method AgentLogger defines.

        A logger implementing exactly the protocol (info/warn/error, e.g. a
        RedisLogger) would otherwise raise AttributeError here — aborting the
        turn on the very notice that is supposed to be benign.  The other tests
        leave the logger unset, so the guard skips the call entirely.
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
        event = {"type": "error", "message": "Reconnecting... 2/5 (stream error)"}
        assert _process(session, event) is None
        assert len(logger.warnings) == 1
        assert "letting it retry" in logger.warnings[0]
