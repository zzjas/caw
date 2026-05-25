"""Tests for cross-process session resume.

A fast unit test exercises the Agent/Session/SessionStore wiring with a fake
provider that mimics the codex case (resume key distinct from session id,
assigned by the backend on first send).  A live test resumes a real claude
session.
"""

from __future__ import annotations

import json

import pytest

from caw import Agent
from caw.agent import _decode_resume_handle, register_provider
from caw.models import TextBlock, Trajectory, Turn, UsageStats
from caw.provider import Provider, ProviderSession

# ===========================================================================
# Fake provider — resume key is distinct from the caw session id (like codex)
# ===========================================================================


def _backend_key(session_id: str) -> str:
    """The backend's own resume key, distinct from caw's session id."""
    return f"thread-for-{session_id}"


class _FakeSession(ProviderSession):
    def __init__(self, session_id, model="fake-model", resume_key=None):
        self._session_id = session_id
        self._model = model
        self._the_resume_key = resume_key  # assigned on first send, like a thread id
        self._created_at = "2026-01-01T00:00:00+00:00"
        self._has_sent = False
        self._turns: list[Turn] = []
        self._total_usage = UsageStats()
        self._total_duration_ms = 0

    def send(self, message: str) -> Turn:
        if not self._has_sent and self._the_resume_key is None:
            # The backend assigns its resume key on the first exchange.
            self._the_resume_key = _backend_key(self._session_id)
        self._has_sent = True
        turn = Turn(
            input=message,
            output=[TextBlock(text=f"reply to: {message}")],
            usage=UsageStats(input_tokens=1, output_tokens=1),
            duration_ms=5,
        )
        self._turns.append(turn)
        self._total_usage = self._total_usage + turn.usage
        self._total_duration_ms += turn.duration_ms
        return turn

    def end(self) -> Trajectory:
        return self.trajectory

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def resume_key(self) -> str | None:
        return self._the_resume_key

    @property
    def last_raw_output(self) -> str:
        return "{}"

    @property
    def trajectory(self) -> Trajectory:
        return Trajectory(
            agent="fake",
            model=self._model,
            session_id=self._session_id,
            created_at=self._created_at,
            turns=list(self._turns),
            usage=self._total_usage,
            duration_ms=self._total_duration_ms,
            metadata={"resume_key": self._the_resume_key} if self._the_resume_key else {},
        )


class _FakeProvider(Provider):
    @property
    def name(self) -> str:
        return "fake"

    def start_session(self, mcp_servers, **kwargs) -> _FakeSession:
        return _FakeSession(session_id=kwargs.get("session_id"), model=kwargs.get("model") or "fake-model")

    def resume_key_from_trajectory(self, trajectory) -> str | None:
        return trajectory.metadata.get("resume_key")

    def resume_session(self, mcp_servers, *, session_id, resume_key, trajectory=None, **kwargs) -> _FakeSession:
        session = _FakeSession(
            session_id=session_id,
            model=kwargs.get("model") or (trajectory.model if trajectory else "fake-model"),
            resume_key=resume_key,
        )
        session._has_sent = True
        if trajectory is not None:
            session._restore_from_trajectory(trajectory)
        return session


@pytest.fixture
def fake_provider():
    register_provider("fake", _FakeProvider)
    yield


# ===========================================================================
# With data_dir: full history restore + append
# ===========================================================================


def test_resume_handle_round_trips(tmp_path, fake_provider):
    """Get a handle, simulate process death, resume in a fresh Agent."""
    agent = Agent(provider="fake", data_dir=str(tmp_path))
    session = agent.start_session()
    session.send("first")
    handle = session.resume_handle
    sid = session.resume_handle and session.trajectory.session_id
    session.end()

    assert isinstance(handle, str)
    # Handle is a self-contained JSON string, not the bare session id.
    assert json.loads(handle)  # parses as JSON
    decoded = _decode_resume_handle(handle)
    assert decoded is not None
    assert decoded["provider"] == "fake"
    assert decoded["session_id"] == sid
    assert decoded["resume_key"] == _backend_key(sid)

    # Brand-new Agent, resume by handle.
    agent2 = Agent(provider="fake", data_dir=str(tmp_path))
    resumed = agent2.resume_session(handle)

    assert resumed._session._has_sent is True
    assert len(resumed.trajectory.turns) == 1  # history restored
    assert resumed._session.resume_key == _backend_key(sid)

    resumed.send("second")
    traj = resumed.trajectory
    assert [t.input for t in traj.turns] == ["first", "second"]
    resumed.end()


def test_resume_appends_to_same_session_dir(tmp_path, fake_provider):
    """Resumed turns continue the original on-disk record, not overwrite it."""
    agent = Agent(provider="fake", data_dir=str(tmp_path))
    session = agent.start_session()
    session.send("turn zero")
    handle = session.resume_handle
    sid = session.trajectory.session_id
    session.end()

    session_dir = tmp_path / "sessions" / sid
    assert (session_dir / "turns" / "000_input.txt").read_text() == "turn zero"

    agent2 = Agent(provider="fake", data_dir=str(tmp_path))
    resumed = agent2.resume_session(handle)
    resumed.send("turn one")
    resumed.end()

    assert (session_dir / "turns" / "000_input.txt").read_text() == "turn zero"
    assert (session_dir / "turns" / "001_input.txt").read_text() == "turn one"

    traj = json.loads((session_dir / "trajectory.json").read_text())
    assert [t["input"] for t in traj["turns"]] == ["turn zero", "turn one"]


def test_resume_accepts_bare_session_id_with_data_dir(tmp_path, fake_provider):
    """A bare caw session id resumes when the session is on disk."""
    agent = Agent(provider="fake", data_dir=str(tmp_path))
    session = agent.start_session()
    session.send("hi")
    sid = session.trajectory.session_id
    session.end()

    agent2 = Agent(provider="fake", data_dir=str(tmp_path))
    resumed = agent2.resume_session(sid)  # bare id, not the token
    assert resumed._session.resume_key == _backend_key(sid)
    assert len(resumed.trajectory.turns) == 1
    resumed.end()


# ===========================================================================
# Without data_dir: backend resumes, trajectory starts fresh
# ===========================================================================


def test_resume_without_data_dir(tmp_path, fake_provider):
    """A self-contained handle resumes the backend even with no data_dir."""
    # Original session persisted to disk so we can grab a real handle.
    agent = Agent(provider="fake", data_dir=str(tmp_path))
    session = agent.start_session()
    session.send("first")
    handle = session.resume_handle
    sid = session.trajectory.session_id
    session.end()

    # Resuming Agent has NO data_dir at all.
    agent2 = Agent(provider="fake", data_dir=None)
    resumed = agent2.resume_session(handle)

    # Backend is resumed (right key, marked as sent)...
    assert resumed._session._has_sent is True
    assert resumed._session.resume_key == _backend_key(sid)
    # ...but caw's trajectory starts empty — no prior turns, no persistence.
    assert len(resumed.trajectory.turns) == 0
    assert resumed.session_dir is None

    resumed.send("second")
    assert [t.input for t in resumed.trajectory.turns] == ["second"]
    resumed.end()


def test_resume_bare_id_without_data_dir_raises(fake_provider):
    """A bare id (no embedded key) cannot resume without disk."""
    agent = Agent(provider="fake", data_dir=None)
    with pytest.raises(FileNotFoundError):
        agent.resume_session("just-a-uuid")


def test_resume_provider_mismatch_raises(tmp_path, fake_provider):
    agent = Agent(provider="fake", data_dir=str(tmp_path))
    session = agent.start_session()
    session.send("hi")
    handle = session.resume_handle
    session.end()

    other = Agent(provider="claude_code", data_dir=str(tmp_path))
    with pytest.raises(ValueError, match="provider"):
        other.resume_session(handle)


def test_resume_handle_requires_a_send(tmp_path, fake_provider):
    """No resume key exists until the backend has been sent to."""
    agent = Agent(provider="fake", data_dir=str(tmp_path))
    session = agent.start_session()
    with pytest.raises(RuntimeError, match="send at least one message"):
        _ = session.resume_handle
    session.end()


# ===========================================================================
# Live integration — real claude session resumed in a fresh Agent
# ===========================================================================


def test_resume_live_claude(tmp_path):
    """Start a claude session, drop it, resume by handle, keep talking."""
    agent = Agent(data_dir=str(tmp_path))
    session = agent.start_session()
    session.send("My favorite number is 42. Acknowledge it.")
    handle = session.resume_handle
    sid = session.trajectory.session_id
    session.end()

    agent2 = Agent(data_dir=str(tmp_path))
    resumed = agent2.resume_session(handle)
    turn = resumed.send("What is my favorite number? Reply with just the number.")
    resumed.end()

    assert "42" in turn.result
    traj = json.loads((tmp_path / "sessions" / sid / "trajectory.json").read_text())
    assert len(traj["turns"]) == 2
