"""Generic logger protocol for caw.

The Agent layer emits a one-line summary for every major event
(tool call, tool result, assistant text, thinking, turn end) through
this interface, in addition to the existing rich-console Display.

Any object with ``info``/``warn``/``error`` string methods satisfies
the protocol — including ``logging.Logger`` (after a tiny adapter for
``warn``→``warning``) and the project's own ``RedisLogger``. Pass an
instance via ``Agent(logger=...)`` or ``agent.start_session(logger=...)``.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

from caw.models import TextBlock, ThinkingBlock, ToolUse, UsageStats


@runtime_checkable
class AgentLogger(Protocol):
    """Minimal logger surface used by caw."""

    def info(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


def _truncate(text: str, max_len: int = 200) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def log_tool_call(logger: AgentLogger | None, block: ToolUse) -> None:
    if logger is None:
        return
    try:
        args = json.dumps(block.arguments, separators=(",", ":"))
    except (TypeError, ValueError):
        args = str(block.arguments)
    logger.info(f"tool_call {block.name} {_truncate(args)}")


def log_tool_result(logger: AgentLogger | None, block: ToolUse) -> None:
    if logger is None:
        return
    tag = "tool_error" if block.is_error else "tool_result"
    logger.info(f"{tag} {block.name} → {_truncate(block.output or '')}")


def log_text(logger: AgentLogger | None, block: TextBlock) -> None:
    if logger is None or not block.text:
        return
    logger.info(f"assistant {_truncate(block.text)}")


def log_thinking(logger: AgentLogger | None, block: ThinkingBlock) -> None:
    if logger is None or not block.text:
        return
    logger.info(f"thinking {_truncate(block.text)}")


def log_turn_end(logger: AgentLogger | None, usage: UsageStats, duration_ms: int) -> None:
    if logger is None:
        return
    parts = [
        f"duration={duration_ms}ms",
        f"tokens={usage.input_tokens}in/{usage.output_tokens}out",
    ]
    if usage.cost_usd:
        parts.append(f"cost=${usage.cost_usd:.4f}")
    logger.info("turn_end " + " ".join(parts))


def log_user_message(logger: AgentLogger | None, message: str) -> None:
    if logger is None:
        return
    logger.info(f"user {_truncate(message)}")


def log_metadata(logger: AgentLogger | None, **kwargs: str) -> None:
    if logger is None:
        return
    pairs = [f"{k}={v}" for k, v in kwargs.items() if v]
    if pairs:
        logger.info("metadata " + " ".join(pairs))
