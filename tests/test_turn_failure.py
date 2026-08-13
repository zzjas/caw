"""Tests for how a failed turn survives serialization and reaches a caller.

``Turn.failure_reason`` exists for the turns a provider deliberately *returns*
while knowing they failed — a partial turn whose tool calls make re-sending
unsafe, and a usage-limited turn the auto-wait loop owns. A provider that
raises never sets it, because there the caller cannot miss the failure.

The question a harness asks is not "did anything ever go wrong" but "did this
run end on a failure", which is why ``Trajectory.failure_reason`` reads the
last turn rather than any turn.
"""

from __future__ import annotations

import json

from caw.models import TextBlock, ToolUse, Trajectory, Turn, UsageStats
from caw.storage import JsonlWriter


def _turn(reason: str | None = None, text: str = "hi") -> Turn:
    return Turn(input="go", output=[TextBlock(text=text)], failure_reason=reason)


class TestTurnField:
    def test_a_turn_defaults_to_not_failed(self):
        turn = Turn(input="go")
        assert turn.failure_reason is None
        assert not turn.failed

    def test_failed_tracks_the_reason(self):
        assert _turn("Request timed out").failed

    def test_a_round_trip_keeps_the_reason(self):
        turn = _turn("error_during_execution after 9 tool call(s); partial turn kept")
        assert Turn.from_dict(turn.to_dict()).failure_reason == turn.failure_reason

    def test_a_clean_turn_serialises_exactly_as_before(self):
        """The key is omitted, not written as null — a trajectory from a clean
        run is byte-identical to one written before this field existed."""
        assert "failure_reason" not in _turn().to_dict()

    def test_a_trajectory_written_before_this_field_still_loads(self):
        legacy = {"input": "go", "output": [{"type": "text", "text": "hi"}], "duration_ms": 5}
        loaded = Turn.from_dict(legacy)
        assert loaded.failure_reason is None
        assert not loaded.failed


class TestTrajectoryView:
    def test_a_run_that_ended_cleanly_reports_nothing(self):
        traj = Trajectory(agent="a", turns=[_turn("boom"), _turn()])
        assert traj.failure_reason is None
        assert not traj.ended_on_failure

    def test_a_run_that_ended_on_a_failure_reports_it(self):
        traj = Trajectory(agent="a", turns=[_turn(), _turn("Request timed out")])
        assert traj.failure_reason == "Request timed out"
        assert traj.ended_on_failure

    def test_an_empty_trajectory_reports_nothing(self):
        assert Trajectory(agent="a").failure_reason is None

    def test_every_failed_turn_is_available_for_auditing(self):
        """A resumed usage limit is a failed turn mid-run and a clean ending —
        both readings must be reachable."""
        traj = Trajectory(agent="a", turns=[_turn("usage limit (error)"), _turn("boom"), _turn()])
        assert len(traj.failed_turns) == 2
        assert not traj.ended_on_failure

    def test_the_reason_survives_a_whole_trajectory_round_trip(self):
        traj = Trajectory(
            agent="a",
            model="m",
            turns=[Turn(input="go", output=[ToolUse(id="1", name="Bash")], failure_reason="timed out")],
            usage=UsageStats(input_tokens=1),
        )
        reloaded = Trajectory.from_dict(json.loads(json.dumps(traj.to_dict())))
        assert reloaded.failure_reason == "timed out"


class TestEventStream:
    def test_turn_end_carries_the_reason(self, tmp_path):
        path = tmp_path / "events.jsonl"
        JsonlWriter(path).write_turn_events(_turn("Request timed out"), turn_index=0)
        ends = [json.loads(ln) for ln in path.read_text().splitlines() if '"turn_end"' in ln]
        assert ends and ends[-1]["failure_reason"] == "Request timed out"

    def test_a_clean_turn_end_is_unchanged(self, tmp_path):
        path = tmp_path / "events.jsonl"
        JsonlWriter(path).write_turn_events(_turn(), turn_index=0)
        ends = [json.loads(ln) for ln in path.read_text().splitlines() if '"turn_end"' in ln]
        assert ends and "failure_reason" not in ends[-1]
