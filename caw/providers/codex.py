"""Codex provider — wraps the ``codex`` CLI in JSON mode."""

from __future__ import annotations

import atexit
import json
import logging
import re
import subprocess
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
from caw.pricing import compute_cost
from caw.provider import Provider, ProviderSession

logger = logging.getLogger(__name__)

# -- Usage-limit detection ----------------------------------------------------

_CODEX_LIMIT_RE = re.compile(
    r"try again at\s+(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)

_DEFAULT_WAIT_MINUTES = 60

# Transient stream-retry notices that codex emits as `error` events while it
# reconnects on its own (e.g. "Reconnecting... 2/5 (stream disconnected before
# completion: ...)"). These must not abort the turn — codex retries internally
# and emits turn.failed only once retries are exhausted.
_TRANSIENT_STREAM_ERROR_RE = re.compile(r"^Reconnecting\.{3}\s+\d+/\d+")


def _parse_codex_reset_minutes(text: str) -> int | None:
    """Parse a Codex limit message and return minutes until reset (+ 5 min buffer).

    Expected format: ``"try again at 3:47 PM"``.
    No timezone is provided so local time is assumed.
    Returns ``None`` if the pattern is not found.
    """
    match = _CODEX_LIMIT_RE.search(text)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    ampm = match.group(3).lower()

    # Convert 12-hour to 24-hour
    if ampm == "am" and hour == 12:
        hour = 0
    elif ampm == "pm" and hour != 12:
        hour += 12

    now = datetime.now()  # local time (Codex doesn't include timezone)
    reset_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if reset_time <= now:
        reset_time += timedelta(days=1)

    delta = reset_time - now
    wait_minutes = int(delta.total_seconds() / 60) + 5  # 5-minute buffer
    return max(1, wait_minutes)


def detect_codex_usage_limit(text: str) -> int | None:
    """Check whether *text* indicates a Codex usage limit.

    Returns the number of minutes to wait before retrying, or ``None`` if no
    limit was detected.
    """
    lower = text.lower()
    if "usage limit" not in lower:
        return None
    return _parse_codex_reset_minutes(text) or _DEFAULT_WAIT_MINUTES


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


# -- MCP config helpers -------------------------------------------------------


def _build_mcp_config_args(mcp_servers: list[MCPServer]) -> list[str]:
    """Build ``-c`` config-override flags wiring up MCP servers for ``codex``.

    Shared by the headless ``exec`` path and the interactive TUI path.
    """
    args: list[str] = []
    for srv in mcp_servers:
        if srv.url:
            args += ["-c", f'mcp_servers.{srv.name}.url="{srv.url}"']
        else:
            args += ["-c", f'mcp_servers.{srv.name}.command="{srv.command}"']
            if srv.args:
                args += ["-c", f"mcp_servers.{srv.name}.args={json.dumps(srv.args)}"]
    return args


class CodexSession(ProviderSession):
    """Live session backed by the ``codex`` CLI."""

    def __init__(
        self,
        mcp_servers: list[MCPServer],
        model: str | None = None,
        system_prompt: str | None = None,
        session_id: str | None = None,
        reasoning: str | None = None,
        sandbox: str | None = None,
    ) -> None:
        self._session_id = session_id or str(uuid.uuid4())
        self._model = model
        self._mcp_servers = mcp_servers
        self._system_prompt = system_prompt
        self._reasoning = reasoning
        self._sandbox = sandbox
        self._created_at = datetime.now(timezone.utc).isoformat()
        self._has_sent = False
        self._thread_id: str | None = None
        self._turns: list[Turn] = []
        self._total_usage = UsageStats()
        self._total_duration_ms = 0
        self._last_raw_output: str = ""
        self._step_callback = None
        self._logger: AgentLogger | None = None

    def set_step_callback(self, callback):
        self._step_callback = callback

    def set_logger(self, logger: AgentLogger | None) -> None:
        self._logger = logger

    # ------------------------------------------------------------------
    # MCP config helpers
    # ------------------------------------------------------------------

    def _mcp_config_args(self) -> list[str]:
        """Build ``-c`` config override flags for MCP servers."""
        return _build_mcp_config_args(self._mcp_servers)

    # ------------------------------------------------------------------
    # Core send (streaming Popen)
    # ------------------------------------------------------------------

    def send(self, message: str) -> Turn:
        display = get_global_display()

        if display:
            if not self._has_sent:
                display.on_metadata(
                    agent="codex",
                    model=self._model or "",
                    session=self._session_id,
                )
            display.on_user_message(message)
        if not self._has_sent:
            log_metadata(
                self._logger,
                agent="codex",
                model=self._model or "",
                session=self._session_id,
            )
        log_user_message(self._logger, message)

        # Build the prompt (prepend system prompt on first turn)
        prompt = message
        if not self._has_sent and self._system_prompt:
            prompt = f"{self._system_prompt}\n\n{message}"

        # Build sandbox flags
        if self._sandbox is None or self._sandbox == "danger-full-access":
            sandbox_flags = ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            sandbox_flags = ["--full-auto", "--sandbox", self._sandbox]

        # Build command
        if not self._has_sent:
            cmd = (
                ["codex", "exec"]
                + sandbox_flags
                + [
                    "--skip-git-repo-check",
                    "--json",
                ]
            )
        else:
            cmd = (
                ["codex", "exec", "resume", self._thread_id or ""]
                + sandbox_flags
                + [
                    "--skip-git-repo-check",
                    "--json",
                ]
            )

        if self._model:
            cmd += ["-m", self._model]

        if self._reasoning:
            cmd += ["-c", f'model_reasoning_effort="{self._reasoning}"']

        cmd += self._mcp_config_args()

        # Prompt as positional arg (last)
        cmd.append(prompt)

        # Accumulated state for event processing
        blocks: list[ContentBlock] = []
        tool_blocks: dict[str, ToolUse] = {}
        usage = UsageStats()
        raw_lines: list[str] = []

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            raise RuntimeError("codex CLI not found. Install it with: npm install -g @openai/codex")

        _register_process(proc)
        try:
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
                    usage = result
                if self._step_callback and blocks:
                    self._step_callback(list(blocks))

            # Read stderr after stdout is exhausted
            stderr = proc.stderr.read() if proc.stderr else ""  # type: ignore[union-attr]
            proc.wait()

            self._last_raw_output = "\n".join(raw_lines)

            if proc.returncode != 0 and not raw_lines:
                raise RuntimeError(f"codex CLI exited with code {proc.returncode}: {stderr}")

        except (KeyboardInterrupt, Exception):
            proc.kill()
            proc.wait()
            raise
        finally:
            _unregister_process(proc)

        self._has_sent = True

        turn = Turn(input=message, output=blocks, usage=usage, duration_ms=0)

        if display:
            display.on_turn_end(turn.result, usage, 0)
        log_turn_end(self._logger, usage, 0)

        self._turns.append(turn)
        self._total_usage = self._total_usage + turn.usage
        return turn

    # ------------------------------------------------------------------
    # Usage-limit detection (called by core Session auto-wait loop)
    # ------------------------------------------------------------------

    def detect_usage_limit(self, turn: Turn) -> int | None:
        """Detect Codex usage-limit messages in the turn's result text."""
        return detect_codex_usage_limit(turn.result)

    # ------------------------------------------------------------------
    # Per-event processing
    # ------------------------------------------------------------------

    def _process_event(
        self,
        event: dict[str, Any],
        blocks: list[ContentBlock],
        tool_blocks: dict[str, ToolUse],
        display: Display | None,
    ) -> UsageStats | None:
        """Process a single JSONL event. Returns UsageStats on ``turn.completed``."""
        event_type = event.get("type")

        if event_type == "thread.started":
            self._thread_id = event.get("thread_id")

        elif event_type == "item.started":
            item = event.get("item", {})
            item_type = item.get("type")
            tool_id = item.get("id", str(uuid.uuid4()))

            if item_type == "command_execution":
                block = ToolUse(
                    id=tool_id,
                    name="command_execution",
                    arguments={"command": item.get("command", "")},
                )
                blocks.append(block)
                tool_blocks[tool_id] = block
                if display:
                    display.on_tool_call(block)
                log_tool_call(self._logger, block)

            elif item_type == "mcp_tool_call":
                server = item.get("server", "")
                tool_name = item.get("tool", "")
                arguments = item.get("arguments", {})
                block = ToolUse(
                    id=tool_id,
                    name=f"{server}.{tool_name}" if server else tool_name,
                    arguments=arguments if isinstance(arguments, dict) else {"input": arguments},
                )
                blocks.append(block)
                tool_blocks[tool_id] = block
                if display:
                    display.on_tool_call(block)
                log_tool_call(self._logger, block)

            elif item_type == "file_change":
                block = ToolUse(
                    id=tool_id,
                    name="file_change",
                    arguments={"file": item.get("file", ""), "action": item.get("action", "")},
                )
                blocks.append(block)
                tool_blocks[tool_id] = block
                if display:
                    display.on_tool_call(block)
                log_tool_call(self._logger, block)

        elif event_type in ("item.completed", "item.updated"):
            item = event.get("item", {})
            item_type = item.get("type")
            is_final = event_type == "item.completed"

            if item_type == "command_execution":
                tool_id = item.get("id", "")
                if tool_id in tool_blocks:
                    tool_blocks[tool_id].output = item.get("output", "")
                    tool_blocks[tool_id].is_error = item.get("exit_code", 0) != 0
                    if display and is_final:
                        display.on_tool_result(tool_blocks[tool_id])
                    if is_final:
                        log_tool_result(self._logger, tool_blocks[tool_id])

            elif item_type == "mcp_tool_call":
                tool_id = item.get("id", "")
                if tool_id in tool_blocks:
                    result = item.get("result")
                    error = item.get("error")
                    if result:
                        # Extract text from MCP content blocks
                        texts: list[str] = []
                        for c in result.get("content", []):
                            if isinstance(c, dict) and c.get("type") == "text":
                                texts.append(c.get("text", ""))
                            elif isinstance(c, str):
                                texts.append(c)
                        tool_blocks[tool_id].output = "\n".join(texts)
                    if error:
                        tool_blocks[tool_id].is_error = True
                        msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                        tool_blocks[tool_id].output = msg
                    elif item.get("status") == "failed":
                        tool_blocks[tool_id].is_error = True
                    if display and is_final:
                        display.on_tool_result(tool_blocks[tool_id])
                    if is_final:
                        log_tool_result(self._logger, tool_blocks[tool_id])

            elif item_type == "file_change":
                tool_id = item.get("id", "")
                if tool_id in tool_blocks:
                    tool_blocks[tool_id].output = item.get("patch", item.get("content", ""))
                    if display and is_final:
                        display.on_tool_result(tool_blocks[tool_id])
                    if is_final:
                        log_tool_result(self._logger, tool_blocks[tool_id])

            elif item_type == "reasoning" and is_final:
                text = item.get("text", "")
                if text:
                    block = ThinkingBlock(text=text)
                    blocks.append(block)
                    if display:
                        display.on_thinking(block)
                    log_thinking(self._logger, block)

            elif item_type == "agent_message" and is_final:
                text = item.get("text", "")
                if text:
                    block = TextBlock(text=text)
                    blocks.append(block)
                    if display:
                        display.on_text(block)
                    log_text(self._logger, block)

        elif event_type == "turn.completed":
            return self._parse_usage(event)

        elif event_type in ("turn.failed", "error"):
            raw = event.get("message", event.get("error", "Unknown error"))
            if isinstance(raw, dict):
                error_msg = raw.get("message", raw.get("error", str(raw)))
            else:
                error_msg = str(raw)
            # Usage-limit errors are recoverable — surface as text so the
            # auto-wait loop in Session.send() can detect and retry.
            if detect_codex_usage_limit(error_msg) is not None:
                block = TextBlock(text=error_msg)
                blocks.append(block)
                if display:
                    display.on_text(block)
                log_text(self._logger, block)
            elif event_type == "error" and _TRANSIENT_STREAM_ERROR_RE.search(error_msg):
                # Codex emits stream-retry notices ("Reconnecting... 2/5 ...")
                # as `error` events while it reconnects on its own. Killing the
                # turn here would abort a session codex was about to recover;
                # if retries are exhausted codex follows up with turn.failed.
                if self._logger:
                    self._logger.warn(f"codex transient stream error, letting it retry: {error_msg}")
            else:
                if self._logger:
                    self._logger.error(f"codex turn failed: {error_msg}")
                raise RuntimeError(f"Codex turn failed: {error_msg}")

        return None

    # ------------------------------------------------------------------
    # Usage parsing
    # ------------------------------------------------------------------

    def _parse_usage(self, event: dict[str, Any]) -> UsageStats:
        u = event.get("usage", {})
        raw_input = u.get("input_tokens", 0)
        cached = u.get("cached_input_tokens", 0)
        usage = UsageStats(
            input_tokens=raw_input - cached,
            output_tokens=u.get("output_tokens", 0),
            cache_read_tokens=cached,
            cache_write_tokens=0,
        )
        usage.cost_usd = compute_cost("codex", self._model or "", usage)
        return usage

    # ------------------------------------------------------------------
    # Trajectory / lifecycle
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def resume_key(self) -> str | None:
        # codex resumes via `codex exec resume <thread_id>`.
        return self._thread_id

    @property
    def last_raw_output(self) -> str:
        return self._last_raw_output

    @property
    def trajectory(self) -> Trajectory:
        return Trajectory(
            agent="codex",
            model=self._model or "",
            session_id=self._session_id,
            created_at=self._created_at,
            system_prompt=self._system_prompt or "",
            reasoning=self._reasoning or "",
            mcp_servers=list(self._mcp_servers),
            turns=list(self._turns),
            usage=self._total_usage,
            duration_ms=self._total_duration_ms,
            # thread_id is the codex-side resume key; persist it so the session
            # can be resumed in a new process (see CodexProvider.resume_session).
            metadata={"thread_id": self._thread_id} if self._thread_id else {},
        )

    def end(self) -> Trajectory:
        return self.trajectory


class CodexProvider(Provider):
    """Provider that delegates to the ``codex`` CLI."""

    @property
    def name(self) -> str:
        return "codex"

    @property
    def binary_name(self) -> str:
        return "codex"

    def check_auth(self):
        from caw.health import codex_auth_signal

        return codex_auth_signal()

    def resolve_model(self, tier: ModelTier) -> str | None:
        from caw.config import get_model

        return get_model("codex", tier)

    def resolve_tool_restrictions(self, tools: ToolGroup) -> dict[str, Any]:
        if tools == ToolGroup.ALL:
            return {}
        if not tools:
            raise ValueError("ToolGroup must not be empty — at least one group is required.")

        has_exec = bool(tools & ToolGroup.EXEC)
        has_writer = bool(tools & ToolGroup.WRITER)
        has_reader = bool(tools & ToolGroup.READER)

        # Warn about groups that Codex cannot distinguish
        lost = []
        for group_name in ("PARALLEL", "WEB", "INTERACTION"):
            group = ToolGroup[group_name]
            if bool(tools & group) != bool(ToolGroup.ALL & group):
                lost.append(group_name)
        if lost:
            logger.warning(
                "Codex provider cannot enforce per-tool restrictions for %s; "
                "these distinctions are lost in sandbox-level mapping.",
                ", ".join(lost),
            )

        if has_exec:
            return {"sandbox": "danger-full-access"}
        if has_writer:
            return {"sandbox": "workspace-write"}
        if has_reader:
            return {"sandbox": "read-only"}

        # Fallback: some groups set but none of READER/WRITER/EXEC
        logger.warning("Codex: no file/exec groups enabled; defaulting to read-only sandbox.")
        return {"sandbox": "read-only"}

    def _limit_probe_kwargs(self) -> dict[str, Any]:
        return {"sandbox": "read-only"}

    def start_interactive(
        self, initial_prompt: str, mcp_servers: list[MCPServer], capture_bytes: int = 0, **kwargs: Any
    ) -> InteractiveResult:
        """Launch ``codex`` interactively (TUI) with an initial prompt.

        Invoking the bare ``codex`` binary with a positional prompt starts its
        full-screen interactive session.  We run it through a pty so the user
        drives the TUI directly while a copy of stdout is captured.

        The sandbox mapping mirrors the headless ``exec`` path (see
        ``CodexSession.send``): an explicit restrictive ``sandbox`` is passed
        through as ``--sandbox``; otherwise codex runs with approvals and the
        sandbox bypassed (``--dangerously-bypass-approvals-and-sandbox``).
        """
        from caw._pty import drive_interactive_pty

        cmd = ["codex"]

        model = kwargs.get("model")
        if model:
            cmd += ["-m", model]

        reasoning = kwargs.get("reasoning")
        if reasoning:
            cmd += ["-c", f'model_reasoning_effort="{reasoning}"']

        sandbox = kwargs.get("sandbox")
        if sandbox is None or sandbox == "danger-full-access":
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            # The user is present in interactive mode, so codex's default
            # approval flow handles escalation; just set the sandbox policy.
            # (Unlike the headless `exec` path, the top-level TUI rejects
            # `--full-auto`.)
            cmd += ["--sandbox", sandbox]

        cmd += _build_mcp_config_args(mcp_servers)

        # codex has no --system-prompt flag; prepend it like the headless path.
        system_prompt = kwargs.get("system_prompt")
        prompt = f"{system_prompt}\n\n{initial_prompt}" if system_prompt else initial_prompt
        cmd.append(prompt)

        return drive_interactive_pty(cmd, capture_bytes=capture_bytes)

    def start_session(self, mcp_servers: list[MCPServer], **kwargs: Any) -> CodexSession:
        return CodexSession(
            mcp_servers=mcp_servers,
            model=kwargs.get("model"),
            system_prompt=kwargs.get("system_prompt"),
            session_id=kwargs.get("session_id"),
            reasoning=kwargs.get("reasoning"),
            sandbox=kwargs.get("sandbox"),
        )

    def resume_key_from_trajectory(self, trajectory: Trajectory) -> str | None:
        return trajectory.metadata.get("thread_id")

    def resume_session(
        self,
        mcp_servers: list[MCPServer],
        *,
        session_id: str,
        resume_key: str,
        trajectory: Trajectory | None = None,
        **kwargs: Any,
    ) -> CodexSession:
        session = CodexSession(
            mcp_servers=mcp_servers,
            model=kwargs.get("model") or (trajectory.model if trajectory else None),
            system_prompt=(trajectory.system_prompt if trajectory else None) or None,
            session_id=session_id,
            reasoning=(trajectory.reasoning if trajectory else None) or None,
            sandbox=kwargs.get("sandbox"),
        )
        # The codex CLI resumes via `codex exec resume <thread_id>`.
        session._thread_id = resume_key
        session._has_sent = True
        if trajectory is not None:
            session._restore_from_trajectory(trajectory)
        return session
