"""Claude Code provider — wraps the ``claude`` CLI in stream-json mode."""

from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from caw.display import Display, get_global_display
from caw.logger import (
    AgentLogger,
    log_metadata,
    log_text,
    log_thinking,
    log_tool_call,
    log_tool_result,
    log_turn_end,
    log_user_message,
)
from caw.models import (
    ContentBlock,
    InteractiveResult,
    MCPServer,
    ModelTier,
    TextBlock,
    ThinkingBlock,
    ToolGroup,
    ToolUse,
    Trajectory,
    Turn,
    UsageStats,
)
from caw.provider import Provider, ProviderSession

# -- Tool group → Claude Code tool name mapping --------------------------------

_TOOL_GROUP_MAP: dict[ToolGroup, list[str]] = {
    ToolGroup.READER: ["Read", "Glob", "Grep"],
    ToolGroup.WRITER: ["Write", "Edit", "NotebookEdit"],
    ToolGroup.EXEC: ["Bash"],
    ToolGroup.WEB: ["WebFetch", "WebSearch"],
    ToolGroup.PARALLEL: ["Task", "TaskOutput", "TaskStop"],
    ToolGroup.INTERACTION: ["AskUserQuestion"],
}

# -- Subprocess registry + atexit cleanup -------------------------------------

_active_processes: set[subprocess.Popen] = set()
_process_lock = threading.Lock()


def _register_process(proc: subprocess.Popen) -> None:
    with _process_lock:
        _active_processes.add(proc)


def _unregister_process(proc: subprocess.Popen) -> None:
    with _process_lock:
        _active_processes.discard(proc)


def _cleanup_processes() -> None:
    """Kill all tracked subprocesses at interpreter exit."""
    with _process_lock:
        procs = list(_active_processes)
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass


atexit.register(_cleanup_processes)

# -- Usage-limit detection ----------------------------------------------------

_LIMIT_RESET_RE = re.compile(
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)",
    re.IGNORECASE,
)
_USAGE_EXHAUSTED_RE = re.compile(r"\bout of (?:extra\s+)?usage\b", re.IGNORECASE)

_DEFAULT_WAIT_MINUTES = 60


def _parse_reset_minutes(text: str) -> int | None:
    """Parse a Claude Code limit message and return minutes until reset (+ 5 min buffer).

    Expected format: ``"resets 3am (UTC)"`` or ``"resets 3:30pm (US/Eastern)"``.
    Returns ``None`` if the pattern is not found.
    """
    match = _LIMIT_RESET_RE.search(text)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    ampm = match.group(3).lower()
    tz_label = match.group(4).strip()

    # Convert 12-hour to 24-hour
    if ampm == "am" and hour == 12:
        hour = 0
    elif ampm == "pm" and hour != 12:
        hour += 12

    if tz_label.upper() == "UTC":
        tz = timezone.utc
    else:
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            tz = ZoneInfo(tz_label)
        except (ImportError, ZoneInfoNotFoundError):
            return None

    now = datetime.now(tz)
    reset_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if reset_time <= now:
        reset_time += timedelta(days=1)

    delta = reset_time - now
    wait_minutes = int(delta.total_seconds() / 60) + 5  # 5-minute buffer
    return max(1, wait_minutes)


def detect_usage_limit(text: str) -> int | None:
    """Check whether *text* indicates a Claude usage limit.

    Returns the number of minutes to wait before retrying, or ``None`` if no
    limit was detected.  When the reset time cannot be parsed from the message,
    the caller-supplied default is used (see ``_DEFAULT_WAIT_MINUTES``).
    """
    lower = text.lower()
    has_limit_phrase = "limit" in lower or _USAGE_EXHAUSTED_RE.search(text) is not None
    if "resets" not in lower or not has_limit_phrase:
        return None
    return _parse_reset_minutes(text) or _DEFAULT_WAIT_MINUTES


class ClaudeCodeSession(ProviderSession):
    """Live session backed by the ``claude`` CLI."""

    #: Provider identifier recorded in trajectories and surfaced to the display.
    #: Subclasses (e.g. the subscription-backed ``claudep`` TUI session) override
    #: this so resume routing and on-screen labels point at the right provider.
    _AGENT_NAME = "claude_code"

    def __init__(
        self,
        mcp_servers: list[MCPServer],
        model: str | None = None,
        system_prompt: str | None = None,
        session_id: str | None = None,
        disallowed_tools: list[str] | None = None,
        reasoning: str | None = None,
    ) -> None:
        self._session_id = session_id or str(uuid.uuid4())
        self._model = model
        self._mcp_servers = mcp_servers
        self._system_prompt = system_prompt
        self._disallowed_tools = disallowed_tools
        self._reasoning = reasoning
        self._created_at = datetime.now(timezone.utc).isoformat()
        self._has_sent = False
        self._turns: list[Turn] = []
        self._total_usage = UsageStats()
        self._total_duration_ms = 0
        self._mcp_config_path: str | None = None
        self._last_raw_output: str = ""
        self._step_callback = None
        self._logger: AgentLogger | None = None

    # ------------------------------------------------------------------
    # MCP config helpers
    # ------------------------------------------------------------------

    def _ensure_mcp_config(self) -> str | None:
        """Write MCP server config to a temp file on first call, return path."""
        if not self._mcp_servers:
            return None
        if self._mcp_config_path is not None:
            return self._mcp_config_path

        config: dict[str, Any] = {"mcpServers": {}}
        for srv in self._mcp_servers:
            if srv.url:
                entry: dict[str, Any] = {"type": "http", "url": srv.url}
            else:
                entry = {"command": srv.command, "args": srv.args}
                if srv.env:
                    entry["env"] = srv.env
            config["mcpServers"][srv.name] = entry

        fd, path = tempfile.mkstemp(suffix=".json", prefix="caw_mcp_")
        with os.fdopen(fd, "w") as f:
            json.dump(config, f)
        self._mcp_config_path = path
        return path

    # ------------------------------------------------------------------
    # Core send (streaming Popen)
    # ------------------------------------------------------------------

    def send(self, message: str) -> Turn:
        display = get_global_display()
        self._emit_send_preamble(message, display)

        cmd = [
            "claude",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
        ]

        if self._disallowed_tools:
            cmd += ["--disallowedTools", ",".join(self._disallowed_tools)]

        if self._model:
            cmd += ["--model", self._model]

        if self._reasoning:
            cmd += ["--effort", self._reasoning]

        if not self._has_sent:
            cmd += ["--session-id", self._session_id]
            if self._system_prompt:
                cmd += ["--system-prompt", self._system_prompt]
        else:
            cmd += ["--resume", self._session_id]

        mcp_path = self._ensure_mcp_config()
        if mcp_path:
            cmd += ["--mcp-config", mcp_path]

        # Accumulated state for event processing
        blocks: list[ContentBlock] = []
        tool_blocks: dict[str, ToolUse] = {}
        usage = UsageStats()
        duration_ms = 0
        raw_lines: list[str] = []

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            raise RuntimeError("claude CLI not found. Install it with: npm install -g @anthropic-ai/claude-code")

        _register_process(proc)
        try:
            # Write message to stdin, then close to signal EOF
            proc.stdin.write(message)  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]

            # Stream stdout line by line
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip("\n")
                raw_lines.append(line)
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    continue

                result = self._process_event(event, blocks, tool_blocks, display)
                if result is not None:
                    usage, duration_ms = result
                if self._step_callback and blocks:
                    self._step_callback(list(blocks))

            # Read stderr after stdout is exhausted
            stderr = proc.stderr.read() if proc.stderr else ""  # type: ignore[union-attr]
            proc.wait()

            self._last_raw_output = "\n".join(raw_lines)

            if proc.returncode != 0 and not raw_lines:
                raise RuntimeError(f"claude CLI exited with code {proc.returncode}: {stderr}")

        except (KeyboardInterrupt, Exception):
            proc.kill()
            proc.wait()
            raise
        finally:
            _unregister_process(proc)

        self._has_sent = True

        turn = Turn(input=message, output=blocks, usage=usage, duration_ms=duration_ms)

        if display:
            display.on_turn_end(turn.result, usage, duration_ms)
        log_turn_end(self._logger, usage, duration_ms)

        self._turns.append(turn)
        self._total_usage = self._total_usage + turn.usage
        self._total_duration_ms += turn.duration_ms
        return turn

    # ------------------------------------------------------------------
    # Usage-limit detection (called by core Session auto-wait loop)
    # ------------------------------------------------------------------

    def detect_usage_limit(self, turn: Turn) -> int | None:
        """Detect Claude Code usage-limit messages in the turn's result text."""
        return detect_usage_limit(turn.result)

    def set_step_callback(self, callback):
        self._step_callback = callback

    def set_logger(self, logger: AgentLogger | None) -> None:
        self._logger = logger

    # ------------------------------------------------------------------
    # Per-event processing
    # ------------------------------------------------------------------

    def _emit_send_preamble(self, message: str, display: Display | None) -> None:
        """Emit the metadata + user-message events at the start of a send().

        Shared by the ``-p`` stream path and the ``claudep`` TUI path so each
        announces itself under its own ``_AGENT_NAME``.
        """
        if display:
            if not self._has_sent:
                display.on_metadata(
                    agent=self._AGENT_NAME,
                    model=self._model or "",
                    session=self._session_id,
                )
            display.on_user_message(message)
        if not self._has_sent:
            log_metadata(
                self._logger,
                agent=self._AGENT_NAME,
                model=self._model or "",
                session=self._session_id,
            )
        log_user_message(self._logger, message)

    def _emit_blocks(
        self,
        new_blocks: list[ContentBlock],
        blocks: list[ContentBlock],
        tool_blocks: dict[str, ToolUse],
        display: Display | None,
    ) -> None:
        """Append parsed blocks to *blocks* and fan them out to display + logger."""
        for block in new_blocks:
            blocks.append(block)
            if isinstance(block, TextBlock):
                if display:
                    display.on_text(block)
                log_text(self._logger, block)
            elif isinstance(block, ThinkingBlock):
                if display:
                    display.on_thinking(block)
                log_thinking(self._logger, block)
            elif isinstance(block, ToolUse):
                if display:
                    display.on_tool_call(block)
                log_tool_call(self._logger, block)
                tool_blocks[block.id] = block

    def _pair_tool_results(
        self,
        event: dict[str, Any],
        tool_blocks: dict[str, ToolUse],
        display: Display | None,
    ) -> None:
        """Attach tool_result payloads from a ``user`` event to their ToolUse.

        Robust to the canonical session JSONL where ``message.content`` may be a
        plain string (the user's own prompt) rather than a list of blocks.
        """
        msg_data = event.get("message", {})
        content_items = msg_data.get("content", [])
        if not isinstance(content_items, list):
            return
        for content in content_items:
            if not isinstance(content, dict) or content.get("type") != "tool_result":
                continue
            tid = content.get("tool_use_id", "")
            if not tid:
                continue
            text_parts: list[str] = []
            raw_content = content.get("content", "")
            if isinstance(raw_content, str):
                text_parts.append(raw_content)
            elif isinstance(raw_content, list):
                for part in raw_content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
            output = "\n".join(text_parts)
            # HTTP MCP transport wraps results in {"result": "..."}
            try:
                parsed = json.loads(output)
                if isinstance(parsed, dict) and "result" in parsed:
                    output = str(parsed["result"])
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            is_error = content.get("is_error", False)
            if tid in tool_blocks:
                tool_blocks[tid].output = output
                tool_blocks[tid].is_error = is_error
                if display:
                    display.on_tool_result(tool_blocks[tid])
                log_tool_result(self._logger, tool_blocks[tid])

    def _process_event(
        self,
        event: dict[str, Any],
        blocks: list[ContentBlock],
        tool_blocks: dict[str, ToolUse],
        display: Display | None,
    ) -> tuple[UsageStats, int] | None:
        """Process a single JSONL event. Returns (usage, duration_ms) on 'result' events."""
        event_type = event.get("type")

        if event_type == "system" and event.get("subtype") == "init":
            if not self._model:
                self._model = event.get("model", "")
                if display and self._model:
                    display.on_metadata(model=self._model)
                if self._model:
                    log_metadata(self._logger, model=self._model)

        elif event_type == "assistant":
            self._emit_blocks(self._parse_assistant_blocks(event), blocks, tool_blocks, display)

        elif event_type == "user":
            # User events carry tool results — pair eagerly
            self._pair_tool_results(event, tool_blocks, display)

        elif event_type == "result":
            return self._parse_usage(event), event.get("duration_ms", 0)

        return None

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_assistant_blocks(event: dict[str, Any]) -> list[ContentBlock]:
        """Parse an assistant event into content blocks."""
        msg_data = event.get("message", {})
        content_blocks = msg_data.get("content", [])
        if not content_blocks:
            return []

        result: list[ContentBlock] = []

        for block in content_blocks:
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text", "")
                if text:
                    result.append(TextBlock(text=text))
            elif block_type == "thinking":
                text = block.get("thinking", "")
                if text:
                    result.append(ThinkingBlock(text=text))
            elif block_type == "tool_use":
                result.append(
                    ToolUse(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=block.get("input", {}),
                    )
                )

        return result

    @staticmethod
    def _parse_usage(event: dict[str, Any]) -> UsageStats:
        u = event.get("usage", {})
        return UsageStats(
            input_tokens=u.get("input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            cache_read_tokens=u.get("cache_read_input_tokens", 0),
            cache_write_tokens=u.get("cache_creation_input_tokens", 0),
            cost_usd=event.get("total_cost_usd", 0.0),
        )

    # ------------------------------------------------------------------
    # Trajectory / lifecycle
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def resume_key(self) -> str | None:
        # claude resumes via `--resume <session_id>`; the key is the id itself.
        return self._session_id

    @property
    def last_raw_output(self) -> str:
        return self._last_raw_output

    @property
    def trajectory(self) -> Trajectory:
        return Trajectory(
            agent=self._AGENT_NAME,
            model=self._model or "",
            session_id=self._session_id,
            created_at=self._created_at,
            system_prompt=self._system_prompt or "",
            mcp_servers=list(self._mcp_servers),
            turns=list(self._turns),
            usage=self._total_usage,
            duration_ms=self._total_duration_ms,
            reasoning=self._reasoning or "",
            metadata={},
        )

    def end(self) -> Trajectory:
        traj = self.trajectory
        if self._mcp_config_path and os.path.exists(self._mcp_config_path):
            os.unlink(self._mcp_config_path)
            self._mcp_config_path = None
        return traj


class ClaudeCodeProvider(Provider):
    """Provider that delegates to the ``claude`` CLI."""

    @property
    def name(self) -> str:
        return "claude_code"

    @property
    def binary_name(self) -> str:
        return "claude"

    def check_auth(self):
        from caw.health import claude_auth_signal

        return claude_auth_signal()

    def resolve_model(self, tier: ModelTier) -> str | None:
        from caw.config import get_model

        return get_model("claude_code", tier)

    def resolve_tool_restrictions(self, tools: ToolGroup) -> dict[str, Any]:
        if tools == ToolGroup.ALL:
            return {}
        if not tools:
            raise ValueError("ToolGroup must not be empty — at least one group is required.")
        disallowed: list[str] = []
        for group, names in _TOOL_GROUP_MAP.items():
            if not (tools & group):
                disallowed.extend(names)
        if not disallowed:
            return {}
        return {"disallowed_tools": disallowed}

    def _limit_probe_kwargs(self) -> dict[str, Any]:
        all_tools: list[str] = []
        for names in _TOOL_GROUP_MAP.values():
            all_tools.extend(names)
        return {"disallowed_tools": all_tools}

    def start_interactive(
        self, initial_prompt: str, mcp_servers: list[MCPServer], capture_bytes: int = 0, **kwargs: Any
    ) -> InteractiveResult:
        from caw._pty import drive_interactive_pty

        cmd = ["claude"]

        model = kwargs.get("model")
        if model:
            cmd += ["--model", model]

        system_prompt = kwargs.get("system_prompt")
        if system_prompt:
            cmd += ["--system-prompt", system_prompt]

        reasoning = kwargs.get("reasoning")
        if reasoning:
            cmd += ["--effort", reasoning]

        disallowed_tools = kwargs.get("disallowed_tools")
        if disallowed_tools:
            cmd += ["--disallowedTools", ",".join(disallowed_tools)]

        # Write MCP config if servers are provided
        mcp_config_path: str | None = None
        if mcp_servers:
            config: dict[str, Any] = {"mcpServers": {}}
            for srv in mcp_servers:
                if srv.url:
                    entry: dict[str, Any] = {"type": "http", "url": srv.url}
                else:
                    entry = {"command": srv.command, "args": srv.args}
                    if srv.env:
                        entry["env"] = srv.env
                config["mcpServers"][srv.name] = entry
            fd, mcp_config_path = tempfile.mkstemp(suffix=".json", prefix="caw_mcp_")
            with os.fdopen(fd, "w") as f:
                json.dump(config, f)
            cmd += ["--mcp-config", mcp_config_path]

        # Initial prompt as positional argument
        cmd.append(initial_prompt)

        def _cleanup() -> None:
            if mcp_config_path and os.path.exists(mcp_config_path):
                os.unlink(mcp_config_path)

        return drive_interactive_pty(cmd, capture_bytes=capture_bytes, on_exit=_cleanup)

    def start_session(self, mcp_servers: list[MCPServer], **kwargs: Any) -> ClaudeCodeSession:
        model = kwargs.get("model")
        system_prompt = kwargs.get("system_prompt")
        session_id = kwargs.get("session_id")
        disallowed_tools = kwargs.get("disallowed_tools")
        reasoning = kwargs.get("reasoning")
        return ClaudeCodeSession(
            mcp_servers=mcp_servers,
            model=model,
            system_prompt=system_prompt,
            session_id=session_id,
            disallowed_tools=disallowed_tools,
            reasoning=reasoning,
        )

    def resume_key_from_trajectory(self, trajectory: Trajectory) -> str | None:
        # claude's resume key is its session id.
        return trajectory.session_id or None

    def resume_session(
        self,
        mcp_servers: list[MCPServer],
        *,
        session_id: str,
        resume_key: str,
        trajectory: Trajectory | None = None,
        **kwargs: Any,
    ) -> ClaudeCodeSession:
        # For claude the resume key *is* the session id (passed to the CLI as
        # --resume once _has_sent is set).
        session = ClaudeCodeSession(
            mcp_servers=mcp_servers,
            model=kwargs.get("model") or (trajectory.model if trajectory else None),
            system_prompt=(trajectory.system_prompt if trajectory else None) or None,
            session_id=resume_key,
            disallowed_tools=kwargs.get("disallowed_tools"),
            reasoning=(trajectory.reasoning if trajectory else None) or None,
        )
        session._has_sent = True
        if trajectory is not None:
            session._restore_from_trajectory(trajectory)
        return session
