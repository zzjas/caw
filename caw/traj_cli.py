"""Terminal viewer for saved CAW trajectories."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import click


class TrajectoryRenderError(ValueError):
    """Raised when a trajectory file cannot be rendered."""


@dataclass
class StepRecord:
    """Indexed visible step within a trajectory."""

    kind: str
    content: str = ""
    blocks: list[dict[str, Any]] = field(default_factory=list)
    name: str = ""
    arguments: Any = field(default_factory=dict)
    result_content: Any = None
    is_error: bool = False
    subagent_trajectory: dict[str, Any] | None = None
    raw_content: Any = None
    line_start: int | None = None
    line_end: int | None = None
    children: list["StepRecord"] = field(default_factory=list)


@dataclass(frozen=True)
class TrajectorySummary:
    """Compact metadata summary for a trajectory."""

    agent: str
    model: str
    turns: int
    top_level_steps: int
    total_steps: int
    tool_errors: int
    duration_ms: int | None
    total_cost_usd: float | None
    total_usage: dict[str, Any]
    tool_usage: dict[str, int]


def _format_size(char_count: int) -> str:
    if char_count >= 1000:
        return f"({char_count / 1000:.1f}k)"
    return f"({char_count}c)"


def _format_count(value: int) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def _format_duration(duration_ms: int | None) -> str | None:
    if duration_ms is None:
        return None
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    seconds = duration_ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {rem:.1f}s"
    hours, rem_minutes = divmod(int(minutes), 60)
    return f"{hours}h {rem_minutes}m"


def _format_cost(cost_usd: float | None) -> str | None:
    if cost_usd is None:
        return None
    if cost_usd == 0:
        return "$0"
    if abs(cost_usd) < 0.01:
        return f"${cost_usd:.4f}"
    return f"${cost_usd:.2f}"


def _format_line_range(start: int | None, end: int | None) -> str:
    if start is None or end is None:
        return "L?"
    if start == end:
        return f"L{start}"
    return f"L{start}-L{end}"


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _normalize_preview(text: str) -> str:
    return " ".join(text.split())


def _stringify(value: Any, *, indent: int | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=indent, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _address_to_text(address: Sequence[int]) -> str:
    return "/".join(str(part) for part in address)


def parse_step_path(text: str) -> tuple[int, ...]:
    """Parse step paths like ``7`` or ``12/3``."""

    candidate = text.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1].strip()
    if not candidate:
        raise ValueError("step path cannot be empty")
    parts = candidate.split("/")
    parsed: list[int] = []
    for part in parts:
        if not part.isdigit():
            raise ValueError(f"invalid step path {text!r}: expected digits separated by '/'")
        parsed.append(int(part))
    return tuple(parsed)


def _expand_range(start: tuple[int, ...], end: tuple[int, ...], raw_text: str) -> list[tuple[int, ...]]:
    if len(start) != len(end):
        raise ValueError(f"invalid step range {raw_text!r}: start and end must have the same depth")
    if start[:-1] != end[:-1]:
        raise ValueError(f"invalid step range {raw_text!r}: start and end must share the same parent")

    lo, hi = start[-1], end[-1]
    step = 1 if hi >= lo else -1
    return [start[:-1] + (idx,) for idx in range(lo, hi + step, step)]


def _parse_step_range(text: str) -> list[tuple[int, ...]]:
    start_text, end_text = [part.strip() for part in text.split("-", 1)]
    start = parse_step_path(start_text)
    if "/" in end_text or (end_text.startswith("[") and end_text.endswith("]")):
        end = parse_step_path(end_text)
    else:
        if not end_text.isdigit():
            raise ValueError(f"invalid step range {text!r}: range end must be a step path")
        end = start[:-1] + (int(end_text),) if len(start) > 1 else (int(end_text),)
    return _expand_range(start, end, text)


def parse_step_selectors(selectors: Sequence[str]) -> list[tuple[int, ...]]:
    """Expand ``--step`` selector expressions into concrete step addresses."""

    addresses: list[tuple[int, ...]] = []
    seen: set[tuple[int, ...]] = set()

    for selector in selectors:
        for item in selector.split(","):
            candidate = item.strip()
            if not candidate:
                continue
            expanded = _parse_step_range(candidate) if "-" in candidate else [parse_step_path(candidate)]
            for address in expanded:
                if address not in seen:
                    seen.add(address)
                    addresses.append(address)

    return addresses


def load_trajectory_file(path: Path) -> tuple[dict[str, Any], str]:
    """Load a JSON trajectory file from disk."""

    try:
        raw = path.read_text()
    except OSError as exc:
        raise TrajectoryRenderError(str(exc)) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TrajectoryRenderError(f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise TrajectoryRenderError("trajectory JSON must contain a top-level object")
    if "turns" not in data and "messages" not in data:
        raise TrajectoryRenderError("trajectory JSON must contain either 'turns' or 'messages'")
    return data, raw


def _assistant_step_from_blocks(blocks: list[dict[str, Any]]) -> StepRecord | None:
    content_parts: list[str] = []
    for block in blocks:
        if block.get("type") in {"text", "thinking"}:
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                content_parts.append(text.strip())
        else:
            content_parts.append(_stringify(block))
    if not blocks and not content_parts:
        return None
    return StepRecord(
        kind="asst",
        content="\n".join(content_parts),
        blocks=list(blocks),
        raw_content=list(blocks),
    )


def _build_steps_from_caw_turns(turns: Any) -> list[StepRecord]:
    if not isinstance(turns, list):
        return []

    steps: list[StepRecord] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue

        user_input = turn.get("input", "")
        if isinstance(user_input, str) and user_input.strip():
            steps.append(StepRecord(kind="user", content=user_input.strip(), raw_content=user_input))

        output_blocks = turn.get("output", [])
        if not isinstance(output_blocks, list):
            continue

        buffered_blocks: list[dict[str, Any]] = []
        for block in output_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                assistant_step = _assistant_step_from_blocks(buffered_blocks)
                if assistant_step is not None:
                    steps.append(assistant_step)
                buffered_blocks = []
                steps.append(
                    StepRecord(
                        kind="tool",
                        name=block.get("name", ""),
                        arguments=block.get("arguments", {}),
                        result_content=block.get("output"),
                        is_error=bool(block.get("is_error", False)),
                        subagent_trajectory=block.get("subagent_trajectory"),
                        raw_content=block,
                    )
                )
            else:
                buffered_blocks.append(block)

        assistant_step = _assistant_step_from_blocks(buffered_blocks)
        if assistant_step is not None:
            steps.append(assistant_step)

    return steps


def _extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", "")).strip()
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
        return "\n".join(texts)
    return ""


def _extract_tool_calls_from_content(content: Any) -> list[dict[str, Any]]:
    tool_calls: list[dict[str, Any]] = []

    if isinstance(content, dict):
        if content.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": content.get("id") or content.get("tool_use_id", ""),
                    "name": content.get("name", ""),
                    "input": content.get("input", {}),
                }
            )
        elif content.get("type") == "tool_bundle":
            for segment in content.get("segments", []):
                if isinstance(segment, dict) and segment.get("type") == "tool_call":
                    tool_calls.append(
                        {
                            "id": segment.get("tool_use_id", ""),
                            "name": segment.get("name", ""),
                            "input": segment.get("input", {}),
                        }
                    )
    elif isinstance(content, list):
        for block in content:
            tool_calls.extend(_extract_tool_calls_from_content(block))

    return tool_calls


def _extract_tool_results_from_content(content: Any) -> list[dict[str, Any]]:
    tool_results: list[dict[str, Any]] = []

    if isinstance(content, dict):
        if content.get("type") == "tool_result":
            tool_results.append(
                {
                    "id": content.get("tool_use_id", ""),
                    "content": content.get("content", ""),
                    "is_error": bool(content.get("is_error", False)),
                }
            )
        elif content.get("type") == "tool_bundle":
            for segment in content.get("segments", []):
                if isinstance(segment, dict) and segment.get("type") == "tool_result":
                    tool_results.append(
                        {
                            "id": segment.get("tool_use_id", ""),
                            "content": segment.get("content", ""),
                            "is_error": bool(segment.get("is_error", False)),
                        }
                    )
    elif isinstance(content, list):
        for block in content:
            tool_results.extend(_extract_tool_results_from_content(block))

    return tool_results


def _parse_tool_result_content(content: Any) -> tuple[str, bool]:
    is_error = False

    if isinstance(content, str):
        if content.startswith("ToolResultBlock("):
            is_error = "is_error=True" in content
        return content, is_error

    if isinstance(content, dict):
        is_error = bool(content.get("is_error", False))
        if "content" in content:
            return _stringify(content.get("content")), is_error
        if "text" in content:
            return _stringify(content.get("text")), is_error
        return _stringify(content), is_error

    if content is None:
        return "", is_error
    return _stringify(content), is_error


def _build_steps_from_messages(messages: Any) -> list[StepRecord]:
    if not isinstance(messages, list):
        return []

    steps: list[StepRecord] = []
    tool_call_index: dict[str, int] = {}

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "")
        content = msg.get("content")

        if role in {"result", "system"}:
            continue

        if role == "user":
            tool_results = _extract_tool_results_from_content(content)
            if tool_results:
                for result in tool_results:
                    tool_id = result.get("id", "")
                    if tool_id in tool_call_index:
                        idx = tool_call_index[tool_id]
                        result_text, parsed_error = _parse_tool_result_content(result.get("content"))
                        steps[idx].result_content = result_text
                        steps[idx].is_error = bool(result.get("is_error") or parsed_error)
            else:
                text = _extract_text_content(content)
                if text:
                    steps.append(StepRecord(kind="user", content=text, raw_content=content))

        elif role == "assistant":
            text = _extract_text_content(content)
            if text:
                steps.append(StepRecord(kind="asst", content=text, raw_content=content))

            tool_calls = _extract_tool_calls_from_content(content)
            for tool_call in tool_calls:
                tool_call_index[tool_call["id"]] = len(steps)
                steps.append(
                    StepRecord(
                        kind="tool",
                        name=tool_call.get("name", ""),
                        arguments=tool_call.get("input", {}),
                        raw_content=content,
                    )
                )

            tool_results = _extract_tool_results_from_content(content)
            for result in tool_results:
                tool_id = result.get("id", "")
                if tool_id in tool_call_index:
                    idx = tool_call_index[tool_id]
                    result_text, parsed_error = _parse_tool_result_content(result.get("content"))
                    steps[idx].result_content = result_text
                    steps[idx].is_error = bool(result.get("is_error") or parsed_error)

        elif role == "tool":
            tool_results = _extract_tool_results_from_content(content)
            if not tool_results and isinstance(content, (str, dict)):
                tool_id = str(msg.get("tool_use_id", ""))
                if tool_id in tool_call_index:
                    idx = tool_call_index[tool_id]
                    result_text, parsed_error = _parse_tool_result_content(content)
                    steps[idx].result_content = result_text
                    steps[idx].is_error = parsed_error
            else:
                for result in tool_results:
                    tool_id = result.get("id", "")
                    if tool_id in tool_call_index:
                        idx = tool_call_index[tool_id]
                        result_text, parsed_error = _parse_tool_result_content(result.get("content"))
                        steps[idx].result_content = result_text
                        steps[idx].is_error = bool(result.get("is_error") or parsed_error)

    return steps


def _prefixed_json_lines(value: Any, indent: int) -> list[str]:
    prefix = " " * indent
    return [prefix + line for line in json.dumps(value, indent=2).splitlines()]


def _find_exact_chunk(
    lines: list[str],
    chunk_lines: list[str],
    start: int,
    end: int | None,
) -> tuple[int, int]:
    if not chunk_lines:
        raise TrajectoryRenderError("cannot locate an empty JSON chunk")

    limit = len(lines) if end is None else min(end, len(lines))
    max_start = limit - len(chunk_lines) + 1
    for idx in range(max(start, 0), max_start):
        matched = True
        for offset, expected in enumerate(chunk_lines):
            actual = lines[idx + offset]
            is_last = offset == len(chunk_lines) - 1
            if actual == expected:
                continue
            if is_last and actual == expected + ",":
                continue
            matched = False
            break
        if matched:
            return idx + 1, idx + len(chunk_lines)
    raise TrajectoryRenderError("could not map visible steps to raw JSON line numbers")


def _find_exact_line(lines: list[str], expected: str, start: int, end: int) -> int:
    for idx in range(max(start, 0), min(end, len(lines))):
        if lines[idx] == expected:
            return idx + 1
    raise TrajectoryRenderError("could not map visible steps to raw JSON line numbers")


def _build_visible_steps_from_caw_turns(
    turns: Any,
    *,
    lines: list[str] | None = None,
    root_indent: int = 0,
    search_start: int = 0,
    search_end: int | None = None,
) -> list[StepRecord]:
    if not isinstance(turns, list):
        return []

    steps: list[StepRecord] = []
    turn_cursor = search_start

    for turn in turns:
        if not isinstance(turn, dict):
            continue

        turn_start = turn_end = None
        input_line = None
        if lines is not None:
            turn_start, turn_end = _find_exact_chunk(
                lines,
                _prefixed_json_lines(turn, root_indent + 4),
                turn_cursor,
                search_end,
            )
            turn_cursor = turn_end
            input_line = _find_exact_line(
                lines,
                " " * (root_indent + 6) + f'"input": {json.dumps(turn.get("input", ""))},',
                turn_start - 1,
                turn_end,
            )

        user_input = turn.get("input", "")
        if isinstance(user_input, str) and user_input.strip():
            steps.append(
                StepRecord(
                    kind="user",
                    content=user_input.strip(),
                    raw_content=user_input,
                    line_start=input_line,
                    line_end=input_line,
                )
            )

        output_blocks = turn.get("output", [])
        if not isinstance(output_blocks, list):
            continue

        block_cursor = (turn_start - 1) if turn_start is not None else 0
        assistant_blocks: list[dict[str, Any]] = []
        assistant_line_start: int | None = None
        assistant_line_end: int | None = None
        anchor_step: StepRecord | None = None

        def _flush_assistant() -> StepRecord | None:
            nonlocal assistant_blocks, assistant_line_start, assistant_line_end, anchor_step
            if not assistant_blocks:
                return None

            content_parts: list[str] = []
            for block in assistant_blocks:
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    content_parts.append(text.strip())

            step = StepRecord(
                kind="asst",
                content="\n".join(content_parts),
                blocks=list(assistant_blocks),
                raw_content=list(assistant_blocks),
                line_start=assistant_line_start,
                line_end=assistant_line_end,
            )
            steps.append(step)
            anchor_step = step
            assistant_blocks = []
            assistant_line_start = None
            assistant_line_end = None
            return step

        for block in output_blocks:
            if not isinstance(block, dict):
                continue

            block_start = block_end = None
            if lines is not None and turn_end is not None:
                block_start, block_end = _find_exact_chunk(
                    lines,
                    _prefixed_json_lines(block, root_indent + 8),
                    block_cursor,
                    turn_end,
                )
                block_cursor = block_end

            block_type = block.get("type", "")
            if block_type in {"text", "thinking"}:
                assistant_blocks.append(block)
                if assistant_line_start is None:
                    assistant_line_start = block_start
                assistant_line_end = block_end
                continue

            if block_type != "tool_use":
                continue

            _flush_assistant()

            subagent = block.get("subagent_trajectory")
            if not isinstance(subagent, dict):
                continue

            if anchor_step is None:
                anchor_step = StepRecord(
                    kind="asst",
                    content="[tool-only assistant output hidden; inspect raw JSON]",
                    line_start=block_start,
                    line_end=block_end,
                )
                steps.append(anchor_step)

            anchor_step.children.extend(
                _build_visible_steps_from_caw_turns(
                    subagent.get("turns", []),
                    lines=lines,
                    root_indent=root_indent + 10,
                    search_start=(block_start - 1) if block_start is not None else 0,
                    search_end=block_end,
                )
            )

        _flush_assistant()

    return steps


def _build_visible_steps_from_messages(messages: Any) -> list[StepRecord]:
    return [step for step in _build_steps_from_messages(messages) if step.kind != "tool"]


def _get_steps(traj: dict[str, Any]) -> list[StepRecord]:
    if "turns" in traj:
        return _build_steps_from_caw_turns(traj.get("turns", []))
    return _build_steps_from_messages(traj.get("messages", []))


def _get_visible_steps(traj: dict[str, Any], raw_text: str | None = None) -> list[StepRecord]:
    if "turns" in traj:
        if raw_text is not None:
            try:
                return _build_visible_steps_from_caw_turns(traj.get("turns", []), lines=raw_text.splitlines())
            except TrajectoryRenderError:
                pass
        return _build_visible_steps_from_caw_turns(traj.get("turns", []))
    return _build_visible_steps_from_messages(traj.get("messages", []))


def _count_visible_steps_recursive(steps: Sequence[StepRecord]) -> int:
    total = 0
    for step in steps:
        total += 1
        total += _count_visible_steps_recursive(step.children)
    return total


def _collect_tool_usage(traj: dict[str, Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for step in _get_steps(traj):
        if step.kind == "tool":
            counts[step.name or "<unknown>"] += 1
            if step.subagent_trajectory:
                counts.update(_collect_tool_usage(step.subagent_trajectory))
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _count_tool_errors(traj: dict[str, Any]) -> int:
    errors = 0
    for step in _get_steps(traj):
        if step.kind == "tool" and step.is_error:
            errors += 1
        if step.subagent_trajectory:
            errors += _count_tool_errors(step.subagent_trajectory)
    return errors


def get_trajectory_summary(
    traj: dict[str, Any],
    *,
    visible_steps: Sequence[StepRecord] | None = None,
) -> TrajectorySummary:
    usage = traj.get("total_usage") or traj.get("usage") or {}
    duration_ms = traj.get("duration_ms")
    total_cost_usd = None
    if isinstance(usage, dict):
        total_cost_usd = usage.get("cost_usd")
    if duration_ms is None or total_cost_usd is None:
        metadata = traj.get("metadata") or traj.get("info") or {}
        if duration_ms is None:
            duration_ms = metadata.get("duration_ms")
        if total_cost_usd is None:
            total_cost_usd = metadata.get("total_cost_usd")

    steps = list(visible_steps) if visible_steps is not None else _get_visible_steps(traj)
    return TrajectorySummary(
        agent=str(traj.get("agent", "")),
        model=str(traj.get("model", "")),
        turns=len(traj.get("turns", []) if isinstance(traj.get("turns"), list) else []),
        top_level_steps=len(steps),
        total_steps=_count_visible_steps_recursive(steps),
        tool_errors=_count_tool_errors(traj),
        duration_ms=duration_ms if isinstance(duration_ms, int) else None,
        total_cost_usd=float(total_cost_usd) if isinstance(total_cost_usd, (int, float)) else None,
        total_usage=usage if isinstance(usage, dict) else {},
        tool_usage=_collect_tool_usage(traj),
    )


def _resolve_step(steps: Sequence[StepRecord], address: Sequence[int]) -> StepRecord:
    current_steps = list(steps)
    current_step: StepRecord | None = None

    for depth, idx in enumerate(address):
        if idx < 0 or idx >= len(current_steps):
            addr_text = _address_to_text(address[: depth + 1])
            raise TrajectoryRenderError(f"step [{addr_text}] does not exist")
        current_step = current_steps[idx]
        if depth == len(address) - 1:
            return current_step
        if not current_step.children:
            addr_text = _address_to_text(address[: depth + 1])
            raise TrajectoryRenderError(f"step [{addr_text}] does not contain nested visible steps")
        current_steps = current_step.children

    raise TrajectoryRenderError("step path cannot be empty")


def render_summary_lines(summary: TrajectorySummary) -> list[str]:
    parts: list[str] = []
    if summary.agent:
        parts.append(f"Agent: {summary.agent}")
    if summary.model:
        parts.append(f"Model: {summary.model}")
    if summary.turns:
        parts.append(f"Turns: {summary.turns}")
    if summary.top_level_steps == summary.total_steps:
        parts.append(f"Steps: {summary.top_level_steps}")
    else:
        parts.append(f"Steps: {summary.top_level_steps} top-level / {summary.total_steps} total")

    usage = summary.total_usage
    if isinstance(usage.get("input_tokens"), int) and usage["input_tokens"]:
        parts.append(f"In: {_format_count(usage['input_tokens'])}")
    if isinstance(usage.get("output_tokens"), int) and usage["output_tokens"]:
        parts.append(f"Out: {_format_count(usage['output_tokens'])}")

    duration = _format_duration(summary.duration_ms)
    if duration:
        parts.append(f"Duration: {duration}")
    cost = _format_cost(summary.total_cost_usd)
    if cost:
        parts.append(f"Cost: {cost}")
    if summary.tool_errors:
        parts.append(f"Tool errors: {summary.tool_errors}")

    tool_usage = "none"
    if summary.tool_usage:
        tool_usage = ", ".join(f"{name}={count}" for name, count in summary.tool_usage.items())

    return [
        " | ".join(parts) if parts else "No summary metadata available.",
        f"Tool usage: {tool_usage}",
    ]


def _render_assistant_detail(step: StepRecord) -> list[str]:
    if not step.blocks:
        return [step.content]

    if len(step.blocks) == 1:
        block = step.blocks[0]
        if block.get("type") == "thinking":
            return ["Thinking:", str(block.get("text", ""))]
        if block.get("type") == "text":
            return [str(block.get("text", ""))]

    lines: list[str] = []
    for index, block in enumerate(step.blocks):
        if index:
            lines.append("")
        block_type = block.get("type", "")
        if block_type == "thinking":
            lines.append("Thinking:")
            lines.append(str(block.get("text", "")))
        elif block_type == "text":
            lines.append("Text:")
            lines.append(str(block.get("text", "")))
        else:
            lines.append(f"{block_type or 'Block'}:")
            lines.append(_stringify(block, indent=2))
    return lines


def render_compressed_trajectory(
    steps: Sequence[StepRecord],
    *,
    recursive: bool = False,
    max_input_chars: int = 60,
    max_text_chars: int = 60,
    prefix: Sequence[int] = (),
    indent: str = "",
) -> str:
    lines: list[str] = []
    for idx, step in enumerate(steps):
        address = tuple(prefix) + (idx,)
        label = f"[{_address_to_text(address)}]"
        line_range = _format_line_range(step.line_start, step.line_end)

        if step.kind == "user":
            preview = _truncate(_normalize_preview(step.content), max_text_chars)
            size = _format_size(len(step.content))
            lines.append(f'{indent}{label} user: "{preview}" {size[:-1]}, {line_range})')
        else:
            preview = _truncate(_normalize_preview(step.content), max_text_chars)
            size = _format_size(len(step.content))
            line = f'{indent}{label} asst: "{preview}" {size[:-1]}, {line_range})'
            if step.children:
                line += f" [nested visible steps: {_count_visible_steps_recursive(step.children)}]"
            lines.append(line)

        if recursive and step.children:
            nested = render_compressed_trajectory(
                step.children,
                recursive=True,
                max_input_chars=max_input_chars,
                max_text_chars=max_text_chars,
                prefix=address,
                indent=f"{indent}  ",
            )
            if nested:
                lines.append(nested)

    return "\n".join(lines)


def render_step_details(
    steps: Sequence[StepRecord],
    address: Sequence[int],
    *,
    max_input_chars: int = 60,
    max_text_chars: int = 60,
) -> str:
    del max_input_chars
    del max_text_chars

    step = _resolve_step(steps, address)
    label = f"[{_address_to_text(address)}]"
    lines: list[str] = [f"{label} raw JSON: {_format_line_range(step.line_start, step.line_end)}", ""]

    if step.kind == "user":
        lines.append(f"{label} user:")
        lines.append(step.content)
        return "\n".join(lines)

    lines.append(f"{label} asst:")
    lines.extend(_render_assistant_detail(step))

    if step.children:
        lines.append("")
        lines.append(f"Nested visible steps: {_count_visible_steps_recursive(step.children)}")
        nested = render_compressed_trajectory(step.children, recursive=False, prefix=address, indent="  ")
        if nested:
            lines.append(nested)

    return "\n".join(lines)


def inspect_trajectory(
    path: Path,
    *,
    step: Sequence[str] | None = None,
    recursive: bool = False,
    text_chars: int = 60,
    input_chars: int = 60,
) -> str:
    """Build CLI output for a trajectory inspection request."""

    try:
        traj, raw_text = load_trajectory_file(path)
        step_paths = parse_step_selectors(step or [])
    except (TrajectoryRenderError, ValueError) as exc:
        raise TrajectoryRenderError(str(exc)) from exc

    visible_steps = _get_visible_steps(traj, raw_text)

    output_lines = [f"Source: {path}"]
    output_lines.extend(render_summary_lines(get_trajectory_summary(traj, visible_steps=visible_steps)))
    output_lines.append("")

    try:
        if step_paths:
            rendered = [
                render_step_details(
                    visible_steps,
                    address,
                    max_input_chars=input_chars,
                    max_text_chars=text_chars,
                )
                for address in step_paths
            ]
            output_lines.append("\n\n".join(rendered))
        else:
            output_lines.append(
                render_compressed_trajectory(
                    visible_steps,
                    recursive=recursive,
                    max_input_chars=input_chars,
                    max_text_chars=text_chars,
                )
            )
    except TrajectoryRenderError as exc:
        raise TrajectoryRenderError(str(exc)) from exc

    return "\n".join(line for line in output_lines if line is not None)


@click.command(
    name="caw-traj",
    context_settings={"help_option_names": ["--help"], "max_content_width": 100},
    help="""Inspect a saved trajectory.

Running `caw-traj PATH` prints a compact, step-indexed view of the conversational trajectory so
another agent can see the structure quickly. Tool calls are omitted from the compressed view.

Use `--step` to retrieve full content for a specific visible step. Step paths use the same
addresses shown in the compact output:

- `7` means top-level step 7
- `12/3` means nested visible step 3 under step 12
- `7-10` expands to steps 7, 8, 9, 10
- `12/3-12/7` expands to a nested range within the same parent
- you can copy the bracketed form directly, for example `--step [12/3]`
- multiple selectors can be combined in one flag, for example `--step 7,8,12/3-12/5`

Each compressed step shows a 1-based raw JSON line range like `L41-L48`. LLM agents should feel
free to inspect the raw JSON file directly with those line numbers when the compact view is not
enough.

Examples:
  caw-traj run.json
  caw-traj run.json --recursive
  caw-traj run.json --step 7
  caw-traj run.json --step 7-10
  caw-traj run.json --step 12/3
  caw-traj run.json --step 12/3-12/7
  caw-traj run.json --step 7,8,12/3-12/5
""",
)
@click.argument("path", type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path))
@click.option(
    "--step",
    "-s",
    multiple=True,
    help="Show full details for visible-step selectors like 7, 7-10, or 12/3-12/7.",
)
@click.option(
    "--recursive",
    "-r",
    is_flag=True,
    help="Include nested visible subagent steps in the compressed listing.",
)
@click.option(
    "--text-chars",
    default=60,
    type=click.IntRange(min=10),
    show_default=True,
    help="Maximum characters to show in user/assistant previews.",
)
@click.option(
    "--input-chars",
    default=60,
    type=click.IntRange(min=10),
    show_default=True,
    help="Reserved for future tool-detail rendering; accepted for compatibility.",
)
def app(
    path: Path,
    step: tuple[str, ...],
    recursive: bool,
    text_chars: int,
    input_chars: int,
) -> None:
    """Standalone click command used by the ``caw-traj`` entrypoint."""

    try:
        click.echo(
            inspect_trajectory(
                path,
                step=step,
                recursive=recursive,
                text_chars=text_chars,
                input_chars=input_chars,
            )
        )
    except TrajectoryRenderError as exc:
        raise click.ClickException(str(exc)) from exc


def main() -> None:
    """Run the standalone ``caw-traj`` CLI."""

    app()


if __name__ == "__main__":
    main()
