"""Core data models for the coding agent wrapper."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Union


class ModelTier(enum.Enum):
    """Abstract model selection tiers.

    Each provider maps these to concrete model identifiers:

        agent = Agent(model=ModelTier.STRONGEST)  # provider picks its best model
        agent = Agent(model=ModelTier.FAST)        # provider picks its fast model
        agent = Agent(model="claude-opus-4-6")     # explicit model string still works
    """

    STRONGEST = "strongest"
    FAST = "fast"


class ToolGroup(enum.Flag):
    """Abstract tool permission groups.

    Combine with ``|`` (union) and ``-`` (subtract) to build permission sets:

        ToolGroup.READER | ToolGroup.EXEC          # read + execute only
        ToolGroup.ALL - ToolGroup.WRITER            # everything except writes
        ToolGroup.ALL - ToolGroup.INTERACTION       # default for automated pipelines
    """

    READER = enum.auto()
    WRITER = enum.auto()
    EXEC = enum.auto()
    WEB = enum.auto()
    PARALLEL = enum.auto()
    INTERACTION = enum.auto()

    ALL = READER | WRITER | EXEC | WEB | PARALLEL | INTERACTION
    NO_INTERACTION = READER | WRITER | EXEC | WEB | PARALLEL

    def __sub__(self, other):
        if not isinstance(other, ToolGroup):
            return NotImplemented
        return self & ~other


@dataclass
class MCPServer:
    """Configuration for an MCP server.

    For stdio transport, set ``command``/``args``/``env``.
    For HTTP transport, set ``url`` (command/args/env are ignored).
    """

    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""


@dataclass
class AgentSpec:
    """Configuration for a subagent."""

    name: str = ""
    description: str = ""
    system_prompt: str = ""
    model: str = ""
    reasoning: str = ""
    tools: ToolGroup | None = None
    tool_servers: list[Any] = field(default_factory=list)
    mcp_servers: list[MCPServer] = field(default_factory=list)
    subagents: list["AgentSpec"] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MCPTool:
    """Descriptor for a tool provided by an MCP server."""

    name: str
    description: str = ""
    server: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class TextBlock:
    """A block of text output from the agent."""

    text: str


@dataclass
class ThinkingBlock:
    """A block of thinking/reasoning output from the agent."""

    text: str


@dataclass
class ToolUse:
    """A tool invocation paired with its result."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    output: str = ""
    is_error: bool = False
    subagent_trajectory: Trajectory | None = None


ContentBlock = Union[TextBlock, ThinkingBlock, ToolUse]


@dataclass
class UsageStats:
    """Token usage and cost statistics."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        """Total tokens consumed (input + output)."""
        return self.input_tokens + self.output_tokens

    def __add__(self, other: UsageStats) -> UsageStats:
        return UsageStats(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UsageStats:
        return cls(
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            cache_read_tokens=d.get("cache_read_tokens", 0),
            cache_write_tokens=d.get("cache_write_tokens", 0),
            cost_usd=d.get("cost_usd", 0.0),
        )


@dataclass
class Turn:
    """A single turn: user sends a message, agent responds."""

    input: str
    output: list[ContentBlock] = field(default_factory=list)
    usage: UsageStats = field(default_factory=UsageStats)
    duration_ms: int = 0
    #: Why the CLI reported this turn as failed, when it did — ``None`` for a
    #: turn that completed normally.
    #:
    #: A provider that *raises* on failure never sets this; it exists for the
    #: turns a provider deliberately **returns** while knowing they failed,
    #: which is the only case where the caller cannot tell. Today that is
    #: ``ClaudeCodeSession.send``: a turn that failed after running tools is
    #: kept rather than re-sent (re-sending would repeat those calls), and a
    #: usage-limited turn is handed back for the auto-wait loop to resume.
    #: Both used to come back indistinguishable from a clean turn — the
    #: failure existed only as a log line — so a caller deriving an outcome
    #: from the return value derived a success.
    failure_reason: str | None = None

    @property
    def result(self) -> str:
        """Last text block's content."""
        for block in reversed(self.output):
            if isinstance(block, TextBlock) and block.text:
                return block.text
        return ""

    @property
    def failed(self) -> bool:
        """Whether the CLI reported this turn as failed. See ``failure_reason``."""
        return self.failure_reason is not None

    @property
    def tool_calls(self) -> list[ToolUse]:
        """All tool calls made during this turn."""
        return [b for b in self.output if isinstance(b, ToolUse)]

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "input": self.input,
            "output": [_block_to_dict(b) for b in self.output],
            "usage": self.usage.to_dict(),
            "duration_ms": self.duration_ms,
        }
        # Omitted when absent, like ToolUse.is_error — a clean turn serialises
        # byte-for-byte as it did before this field existed.
        if self.failure_reason is not None:
            d["failure_reason"] = self.failure_reason
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Turn:
        return cls(
            input=d.get("input", ""),
            output=[_block_from_dict(b) for b in d.get("output", [])],
            usage=UsageStats.from_dict(d.get("usage", {})),
            duration_ms=d.get("duration_ms", 0),
            failure_reason=d.get("failure_reason"),
        )


@dataclass
class Trajectory:
    """Complete record of a session.

    ``usage`` tracks this agent's own token usage. Use ``total_usage`` to get
    the accumulated usage including all nested subagent trajectories.
    """

    agent: str
    model: str = ""
    session_id: str = ""
    created_at: str = ""
    completed_at: str = ""
    usage_limited: bool = False
    system_prompt: str = ""
    reasoning: str = ""
    mcp_servers: list[MCPServer] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    usage: UsageStats = field(default_factory=UsageStats)
    duration_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def num_turns(self) -> int:
        return len(self.turns)

    @property
    def result(self) -> str:
        """The final result from the last turn."""
        if self.turns:
            return self.turns[-1].result
        return ""

    @property
    def total_tool_calls(self) -> int:
        return sum(len(t.tool_calls) for t in self.turns)

    @property
    def failure_reason(self) -> str | None:
        """Why the session's **last** turn failed, or ``None`` if it didn't.

        The question a caller recording an outcome has: not "did anything ever
        go wrong" but "did this run end on a failure". A mid-run turn that
        failed and was retried never reaches the trajectory, and a
        usage-limited turn that the auto-wait loop resumed past is followed by
        the clean turn that replaced it — so both correctly read ``None`` here.
        """
        return self.turns[-1].failure_reason if self.turns else None

    @property
    def ended_on_failure(self) -> bool:
        """Whether the last turn failed. See ``failure_reason``."""
        return self.failure_reason is not None

    @property
    def failed_turns(self) -> list[Turn]:
        """Every turn the CLI reported as failed, in order.

        For auditing a whole run; ``ended_on_failure`` is the outcome question.
        """
        return [t for t in self.turns if t.failed]

    @property
    def total_usage(self) -> UsageStats:
        """Accumulated usage: own + all nested subagent trajectories (recursive)."""
        total = self.usage
        for turn in self.turns:
            for block in turn.output:
                if isinstance(block, ToolUse) and block.subagent_trajectory:
                    total = total + block.subagent_trajectory.total_usage
        return total

    @property
    def subagent_trajectories(self) -> list[Trajectory]:
        """All subagent trajectories across all turns."""
        trajs: list[Trajectory] = []
        for turn in self.turns:
            for block in turn.output:
                if isinstance(block, ToolUse) and block.subagent_trajectory:
                    trajs.append(block.subagent_trajectory)
        return trajs

    @property
    def is_usage_limited(self) -> bool:
        """Whether the session ended due to a usage limit.

        Set by ``Session.end()`` using the provider's ``detect_usage_limit``.
        """
        return self.usage_limited

    @property
    def is_complete(self) -> bool:
        """Whether the session completed normally.

        A trajectory is complete when it has been finalized (``completed_at``
        is set by ``Session.end()``) and was not usage-limited.  Mid-session
        snapshots written by ``append_turn`` have an empty ``completed_at``
        and are therefore not considered complete.
        """
        return bool(self.completed_at) and not self.is_usage_limited

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "model": self.model,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "usage_limited": self.usage_limited,
            "system_prompt": self.system_prompt,
            "reasoning": self.reasoning,
            "mcp_servers": [
                {"name": s.name, "command": s.command, "args": s.args, "env": s.env, "url": s.url}
                for s in self.mcp_servers
            ],
            "turns": [t.to_dict() for t in self.turns],
            "usage": self.usage.to_dict(),
            "total_usage": self.total_usage.to_dict(),
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Trajectory:
        return cls(
            agent=d.get("agent", ""),
            model=d.get("model", ""),
            session_id=d.get("session_id", ""),
            created_at=d.get("created_at", ""),
            completed_at=d.get("completed_at", ""),
            usage_limited=d.get("usage_limited", False),
            system_prompt=d.get("system_prompt", ""),
            reasoning=d.get("reasoning", ""),
            mcp_servers=[
                MCPServer(
                    name=s.get("name", ""),
                    command=s.get("command", ""),
                    args=s.get("args", []),
                    env=s.get("env", {}),
                    url=s.get("url", ""),
                )
                for s in d.get("mcp_servers", [])
            ],
            turns=[Turn.from_dict(t) for t in d.get("turns", [])],
            usage=UsageStats.from_dict(d.get("usage", {})),
            duration_ms=d.get("duration_ms", 0),
            metadata=d.get("metadata", {}),
        )


# -- Serialization helpers for content blocks --------------------------------


@dataclass
class InteractiveResult:
    """Result from an interactive agent session."""

    exit_code: int
    output: str = ""  # raw terminal output (may include ANSI escape sequences)

    @property
    def session_id(self) -> str | None:
        """Extract the session ID from Claude Code's exit output, if present."""
        import re

        m = re.search(r"--resume\s+(\S+)", self.output)
        return m.group(1) if m else None


def _block_to_dict(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    elif isinstance(block, ThinkingBlock):
        return {"type": "thinking", "text": block.text}
    else:  # ToolUse
        d: dict[str, Any] = {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "arguments": block.arguments,
            "output": block.output,
        }
        if block.is_error:
            d["is_error"] = True
        if block.subagent_trajectory:
            d["subagent_trajectory"] = block.subagent_trajectory.to_dict()
        return d


def _block_from_dict(d: dict[str, Any]) -> ContentBlock:
    btype = d.get("type", "")
    if btype == "text":
        return TextBlock(text=d.get("text", ""))
    elif btype == "thinking":
        return ThinkingBlock(text=d.get("text", ""))
    else:  # tool_use
        sub_traj = None
        if d.get("subagent_trajectory"):
            sub_traj = Trajectory.from_dict(d["subagent_trajectory"])
        return ToolUse(
            id=d.get("id", ""),
            name=d.get("name", ""),
            arguments=d.get("arguments", {}),
            output=d.get("output", ""),
            is_error=d.get("is_error", False),
            subagent_trajectory=sub_traj,
        )
