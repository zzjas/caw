"""Tests for the ``caw-traj`` CLI."""

from __future__ import annotations

import json

from click.testing import CliRunner

from caw.models import TextBlock, ThinkingBlock, ToolUse, Trajectory, Turn, UsageStats
from caw.traj_cli import app


runner = CliRunner()


def _write_traj(tmp_path) -> str:
    subagent = Trajectory(
        agent="reviewer",
        model="gpt-5-mini",
        session_id="sub-1",
        created_at="2026-01-01T00:00:00Z",
        turns=[
            Turn(
                input="Review auth.py",
                output=[
                    ThinkingBlock(text="I should inspect the file first."),
                    TextBlock(text="Reading auth.py."),
                    ToolUse(
                        id="sub-tool-1",
                        name="Read",
                        arguments={"path": "auth.py"},
                        output="def login():\n    return True\n",
                    ),
                ],
                usage=UsageStats(input_tokens=20, output_tokens=15),
                duration_ms=150,
            )
        ],
        usage=UsageStats(input_tokens=20, output_tokens=15, cost_usd=0.0025),
        duration_ms=150,
    )

    traj = Trajectory(
        agent="parent",
        model="gpt-5",
        session_id="top-1",
        created_at="2026-01-01T00:00:00Z",
        turns=[
            Turn(
                input="Review the auth module.",
                output=[
                    TextBlock(text="Delegating a focused review."),
                    ToolUse(
                        id="tool-1",
                        name="delegate",
                        arguments={"task": "review auth"},
                        output="delegated",
                        subagent_trajectory=subagent,
                    ),
                ],
                usage=UsageStats(input_tokens=100, output_tokens=50),
                duration_ms=500,
            )
        ],
        usage=UsageStats(input_tokens=100, output_tokens=50, cost_usd=0.01),
        duration_ms=500,
    )

    path = tmp_path / "traj.json"
    path.write_text(json.dumps(traj.to_dict(), indent=2))
    return str(path)


def test_help_explains_step_paths():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "calls are omitted from the compressed view" in result.output
    assert "--step" in result.output
    assert "12/3" in result.output
    assert "7-10" in result.output
    assert "inspect the raw JSON file directly" in result.output


def test_default_output_is_compressed(tmp_path):
    path = _write_traj(tmp_path)

    result = runner.invoke(app, [path])

    assert result.exit_code == 0
    assert f"Source: {path}" in result.output
    assert "Agent: parent" in result.output
    assert '[0] user: "Review the auth module." (23c, L13)' in result.output
    assert '[1] asst: "Delegating a focused review." (28c, L15-L18)' in result.output
    assert "[nested visible steps: 2]" in result.output
    assert "\n[2]" not in result.output
    assert "\n[1/2]" not in result.output


def test_recursive_output_includes_nested_addresses(tmp_path):
    path = _write_traj(tmp_path)

    result = runner.invoke(app, [path, "--recursive"])

    assert result.exit_code == 0
    assert '[1/0] user: "Review auth.py" (14c, L39)' in result.output
    assert '[1/1] asst: "I should inspect the file first. Reading auth.py." (49c, L41-L48)' in result.output
    assert "Read:" not in result.output


def test_step_detail_shows_subagent_overview(tmp_path):
    path = _write_traj(tmp_path)

    result = runner.invoke(app, [path, "--step", "1"])

    assert result.exit_code == 0
    assert "[1] raw JSON: L15-L18" in result.output
    assert "[1] asst:" in result.output
    assert "Delegating a focused review." in result.output
    assert "Nested visible steps: 2" in result.output
    assert '[1/0] user: "Review auth.py" (14c, L39)' in result.output


def test_nested_step_detail_uses_step_path(tmp_path):
    path = _write_traj(tmp_path)

    result = runner.invoke(app, [path, "--step", "1/1"])

    assert result.exit_code == 0
    assert "[1/1] raw JSON: L41-L48" in result.output
    assert "[1/1] asst:" in result.output
    assert "Thinking:" in result.output
    assert "Reading auth.py." in result.output


def test_step_range_expands_multiple_details(tmp_path):
    path = _write_traj(tmp_path)

    result = runner.invoke(app, [path, "--step", "0-1"])

    assert result.exit_code == 0
    assert "[0] user:" in result.output
    assert "[1] asst:" in result.output


def test_mixed_selector_list_supports_nested_ranges(tmp_path):
    path = _write_traj(tmp_path)

    result = runner.invoke(app, [path, "--step", "0,1/0-1/1"])

    assert result.exit_code == 0
    assert "[0] user:" in result.output
    assert "[1/0] user:" in result.output
    assert "[1/1] asst:" in result.output


def test_invalid_step_path_exits_nonzero(tmp_path):
    path = _write_traj(tmp_path)

    result = runner.invoke(app, [path, "--step", "9"])

    assert result.exit_code == 1
    assert "step [9] does not exist" in result.output


def test_invalid_cross_parent_range_exits_nonzero(tmp_path):
    path = _write_traj(tmp_path)

    result = runner.invoke(app, [path, "--step", "1/0-0/0"])

    assert result.exit_code == 1
    assert "must share the same parent" in result.output
