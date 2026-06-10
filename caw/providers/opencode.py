"""opencode provider — wraps the ``opencode`` CLI in stream-JSON (`--format json`) mode."""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
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


# -- Binary discovery ---------------------------------------------------------

# Common install locations that may not be on PATH (the official installer
# drops the binary in ``~/.opencode/bin/opencode``).
_OPENCODE_FALLBACKS = [
    Path.home() / ".opencode" / "bin" / "opencode",
    Path("/usr/local/bin/opencode"),
    Path("/opt/homebrew/bin/opencode"),
]


def _resolve_opencode_path() -> str | None:
    """Resolve the opencode CLI path; prefer PATH, fall back to install dirs.

    Returns ``None`` when not found anywhere.
    """
    found = shutil.which("opencode")
    if found:
        return found
    for path in _OPENCODE_FALLBACKS:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _find_opencode_binary() -> str:
    """Locate the opencode CLI for launching a subprocess.

    Falls back to the bare name ``"opencode"`` when not found, so the
    subprocess raises ``FileNotFoundError`` → friendly RuntimeError.
    """
    return _resolve_opencode_path() or "opencode"


# -- Usage-limit detection ----------------------------------------------------

_DEFAULT_WAIT_MINUTES = 60


def detect_opencode_usage_limit(text: str) -> int | None:
    """Check whether *text* indicates an opencode usage-limit message.

    opencode surfaces upstream-provider usage limits via ``session.error``
    events (``FreeUsageLimitError`` / ``GoUsageLimitError``).  When such an
    error message hits the agent's text/error stream, this function returns
    the number of minutes to wait before retrying.  Returns ``None`` if no
    limit was detected.
    """
    lower = text.lower()
    if "usagelimit" not in lower and "usage limit" not in lower:
        return None
    # Try to extract a retry-after duration if present
    m = re.search(r"retry[\s-]*after[:\s]+(\d+)", lower)
    if m:
        minutes = max(1, int(m.group(1)) // 60 + 1)
        return minutes
    return _DEFAULT_WAIT_MINUTES


# -- Tool group → permission map ----------------------------------------------

# opencode tool ids (see opencode/packages/opencode/src/tool/registry.ts):
#   bash, edit, write, glob, grep, read, task, todowrite, webfetch, websearch
# Note: the "question" tool is auto-denied by opencode in non-interactive run.

_TOOL_GROUP_MAP: dict[ToolGroup, list[str]] = {
    ToolGroup.READER: ["read", "glob", "grep"],
    ToolGroup.WRITER: ["edit", "write"],
    ToolGroup.EXEC: ["bash"],
    ToolGroup.WEB: ["webfetch", "websearch"],
    ToolGroup.PARALLEL: ["task"],
    ToolGroup.INTERACTION: [],  # already auto-denied by opencode in non-interactive mode
}


class OpencodeSession(ProviderSession):
    """Live session backed by the ``opencode`` CLI."""

    def __init__(
        self,
        mcp_servers: list[MCPServer],
        model: str | None = None,
        system_prompt: str | None = None,
        session_id: str | None = None,
        reasoning: str | None = None,
        disabled_tools: list[str] | None = None,
    ) -> None:
        # caw-side bookkeeping id; opencode generates its own session id which
        # we capture from the first event stream and reuse on subsequent turns.
        self._session_id = session_id or str(uuid.uuid4())
        self._opencode_session_id: str | None = None
        self._model = model
        self._mcp_servers = mcp_servers
        self._system_prompt = system_prompt
        self._reasoning = reasoning
        self._disabled_tools = disabled_tools or []
        self._created_at = datetime.now(timezone.utc).isoformat()
        self._has_sent = False
        self._turns: list[Turn] = []
        self._total_usage = UsageStats()
        self._total_duration_ms = 0
        self._config_path: str | None = None
        self._last_raw_output: str = ""
        self._step_callback = None
        self._logger: AgentLogger | None = None

    # ------------------------------------------------------------------
    # Logger / step callback hooks
    # ------------------------------------------------------------------

    def set_step_callback(self, callback):
        self._step_callback = callback

    def set_logger(self, logger: AgentLogger | None) -> None:
        self._logger = logger

    # ------------------------------------------------------------------
    # Config helpers (MCP + tool restrictions via OPENCODE_CONFIG file)
    # ------------------------------------------------------------------

    def _ensure_config(self) -> str | None:
        """Write an opencode config containing MCP servers + tool toggles.

        Returns the path to write, or ``None`` if no config is needed.
        opencode reads this path when ``OPENCODE_CONFIG`` is set in env.
        """
        if not self._mcp_servers and not self._disabled_tools:
            return None
        if self._config_path is not None:
            return self._config_path

        config: dict[str, Any] = {"$schema": "https://opencode.ai/config.json"}

        if self._mcp_servers:
            mcp: dict[str, Any] = {}
            for srv in self._mcp_servers:
                if srv.url:
                    entry: dict[str, Any] = {"type": "remote", "url": srv.url}
                else:
                    cmd_list = [srv.command] + list(srv.args or [])
                    entry = {"type": "local", "command": cmd_list}
                    if srv.env:
                        entry["environment"] = srv.env
                mcp[srv.name] = entry
            config["mcp"] = mcp

        if self._disabled_tools:
            config["tools"] = {name: False for name in self._disabled_tools}

        fd, path = tempfile.mkstemp(suffix=".json", prefix="caw_opencode_")
        with os.fdopen(fd, "w") as f:
            json.dump(config, f)
        self._config_path = path
        return path

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    def send(self, message: str) -> Turn:
        display = get_global_display()

        if display:
            if not self._has_sent:
                display.on_metadata(
                    agent="opencode",
                    model=self._model or "",
                    session=self._session_id,
                )
            display.on_user_message(message)
        if not self._has_sent:
            log_metadata(
                self._logger,
                agent="opencode",
                model=self._model or "",
                session=self._session_id,
            )
        log_user_message(self._logger, message)

        # Build the prompt (prepend system prompt on first turn — opencode
        # has no CLI flag for system prompt; mirrors codex behavior).
        prompt = message
        if not self._has_sent and self._system_prompt:
            prompt = f"{self._system_prompt}\n\n{message}"

        cmd = [
            _find_opencode_binary(),
            "run",
            "--format",
            "json",
            "--dangerously-skip-permissions",
        ]

        if self._opencode_session_id:
            cmd += ["--session", self._opencode_session_id]

        if self._model:
            cmd += ["--model", self._model]

        if self._reasoning:
            cmd += ["--variant", self._reasoning]

        # Emit thinking blocks too
        cmd += ["--thinking"]

        # Prompt as positional arg (last)
        cmd.append(prompt)

        env = os.environ.copy()
        config_path = self._ensure_config()
        if config_path:
            env["OPENCODE_CONFIG"] = config_path

        # Accumulated state for event processing
        blocks: list[ContentBlock] = []
        tool_blocks: dict[str, ToolUse] = {}
        usage = UsageStats()
        raw_lines: list[str] = []
        turn_start_ms = int(datetime.now().timestamp() * 1000)

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "opencode CLI not found. Install it from https://opencode.ai or make sure the binary is on PATH."
            )

        _register_process(proc)
        try:
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
                    # opencode emits one step_finish per step (tool calls
                    # produce multiple steps per turn); accumulate.
                    usage = usage + result
                if self._step_callback and blocks:
                    self._step_callback(list(blocks))

            stderr = proc.stderr.read() if proc.stderr else ""  # type: ignore[union-attr]
            proc.wait()

            self._last_raw_output = "\n".join(raw_lines)

            if proc.returncode != 0 and not raw_lines:
                raise RuntimeError(f"opencode CLI exited with code {proc.returncode}: {stderr}")

        except (KeyboardInterrupt, Exception):
            proc.kill()
            proc.wait()
            raise
        finally:
            _unregister_process(proc)

        self._has_sent = True
        turn_end_ms = int(datetime.now().timestamp() * 1000)
        duration_ms = turn_end_ms - turn_start_ms

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
        return detect_opencode_usage_limit(turn.result)

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
        """Process a single JSON event from `opencode run --format json`."""
        event_type = event.get("type")

        # Capture opencode's session ID from the first event we see
        if self._opencode_session_id is None:
            sid = event.get("sessionID")
            if isinstance(sid, str) and sid:
                self._opencode_session_id = sid

        if event_type == "step_start":
            # Marks the beginning of a new model step. Nothing actionable yet.
            return None

        if event_type == "text":
            part = event.get("part", {})
            text = part.get("text", "")
            if not text:
                return None
            block = TextBlock(text=text)
            blocks.append(block)
            if display:
                display.on_text(block)
            log_text(self._logger, block)
            return None

        if event_type == "reasoning":
            part = event.get("part", {})
            text = part.get("text", "")
            # For providers that encrypt reasoning (e.g. OpenAI), text is empty;
            # surface a placeholder so the trajectory records that thinking happened.
            if not text:
                meta = part.get("metadata", {}) or {}
                if any("reasoningEncryptedContent" in (v or {}) for v in meta.values() if isinstance(v, dict)):
                    text = "[encrypted reasoning]"
            if not text:
                return None
            block = ThinkingBlock(text=text)
            blocks.append(block)
            if display:
                display.on_thinking(block)
            log_thinking(self._logger, block)
            return None

        if event_type == "tool_use":
            part = event.get("part", {})
            if part.get("type") != "tool":
                return None
            call_id = part.get("callID") or part.get("id") or str(uuid.uuid4())
            state = part.get("state", {}) or {}
            status = state.get("status")
            is_error = status == "error"

            # Tool block may have been emitted earlier in a "running" event; check
            tool_block = tool_blocks.get(call_id)
            if tool_block is None:
                tool_block = ToolUse(
                    id=call_id,
                    name=part.get("tool", "tool"),
                    arguments=state.get("input", {}) or {},
                )
                blocks.append(tool_block)
                tool_blocks[call_id] = tool_block
                if display:
                    display.on_tool_call(tool_block)
                log_tool_call(self._logger, tool_block)

            # Fill in output / error
            output = state.get("output", "")
            if is_error:
                err = state.get("error")
                if isinstance(err, dict):
                    output = err.get("message", json.dumps(err))
                elif err:
                    output = str(err)
                elif not output:
                    output = "tool call failed"
            tool_block.output = output if isinstance(output, str) else json.dumps(output)
            tool_block.is_error = is_error

            if display:
                display.on_tool_result(tool_block)
            log_tool_result(self._logger, tool_block)
            return None

        if event_type == "step_finish":
            part = event.get("part", {})
            tokens = part.get("tokens", {}) or {}
            cache = tokens.get("cache", {}) or {}
            input_tokens = int(tokens.get("input", 0) or 0)
            output_tokens = int(tokens.get("output", 0) or 0)
            reasoning_tokens = int(tokens.get("reasoning", 0) or 0)
            cache_read = int(cache.get("read", 0) or 0)
            cache_write = int(cache.get("write", 0) or 0)
            usage = UsageStats(
                input_tokens=input_tokens,
                # opencode separates "output" and "reasoning"; bill reasoning at
                # output rate per OpenAI/Anthropic conventions.
                output_tokens=output_tokens + reasoning_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                cost_usd=float(part.get("cost", 0.0) or 0.0),
            )
            # Fall back to caw pricing when opencode reports zero cost (common
            # for OAuth/subscription users where the bill is server-side).
            if usage.cost_usd == 0.0 and self._model:
                usage.cost_usd = compute_cost("opencode", self._model, usage)
            # Aggregate across multiple step_finish events in the same turn
            return usage

        if event_type == "error":
            err = event.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("name") or json.dumps(err)
            else:
                msg = str(err) if err else "opencode error"
            # Usage-limit errors are recoverable — surface as text so the
            # caw auto-wait loop in Session.send() can detect and retry.
            if detect_opencode_usage_limit(msg) is not None:
                block = TextBlock(text=msg)
                blocks.append(block)
                if display:
                    display.on_text(block)
                log_text(self._logger, block)
            else:
                if self._logger:
                    self._logger.error(f"opencode error: {msg}")
                raise RuntimeError(f"opencode error: {msg}")
            return None

        return None

    # ------------------------------------------------------------------
    # Trajectory / lifecycle
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def resume_key(self) -> str | None:
        # opencode resumes via `--session <opencode_session_id>` (the CLI's own
        # id, captured from the event stream — distinct from caw's session_id).
        return self._opencode_session_id

    @property
    def last_raw_output(self) -> str:
        return self._last_raw_output

    @property
    def trajectory(self) -> Trajectory:
        return Trajectory(
            agent="opencode",
            model=self._model or "",
            session_id=self._session_id,
            created_at=self._created_at,
            system_prompt=self._system_prompt or "",
            reasoning=self._reasoning or "",
            mcp_servers=list(self._mcp_servers),
            turns=list(self._turns),
            usage=self._total_usage,
            duration_ms=self._total_duration_ms,
            metadata={"opencode_session_id": self._opencode_session_id} if self._opencode_session_id else {},
        )

    def end(self) -> Trajectory:
        traj = self.trajectory
        if self._config_path and os.path.exists(self._config_path):
            try:
                os.unlink(self._config_path)
            except OSError:
                pass
            self._config_path = None
        return traj


class OpencodeProvider(Provider):
    """Provider that delegates to the ``opencode`` CLI."""

    @property
    def name(self) -> str:
        return "opencode"

    @property
    def binary_name(self) -> str:
        return "opencode"

    def find_binary(self) -> str | None:
        return _resolve_opencode_path()

    def check_auth(self):
        from caw.health import opencode_auth_signal

        return opencode_auth_signal()

    def resolve_model(self, tier: ModelTier) -> str | None:
        from caw.config import get_model

        return get_model("opencode", tier)

    def resolve_tool_restrictions(self, tools: ToolGroup) -> dict[str, Any]:
        if tools == ToolGroup.ALL:
            return {}
        if not tools:
            raise ValueError("ToolGroup must not be empty — at least one group is required.")
        disabled: list[str] = []
        for group, names in _TOOL_GROUP_MAP.items():
            if not (tools & group):
                disabled.extend(names)
        if not disabled:
            return {}
        return {"disabled_tools": disabled}

    def _limit_probe_kwargs(self) -> dict[str, Any]:
        all_tools: list[str] = []
        for names in _TOOL_GROUP_MAP.values():
            all_tools.extend(names)
        return {"disabled_tools": all_tools}

    def start_session(self, mcp_servers: list[MCPServer], **kwargs: Any) -> OpencodeSession:
        return OpencodeSession(
            mcp_servers=mcp_servers,
            model=kwargs.get("model"),
            system_prompt=kwargs.get("system_prompt"),
            session_id=kwargs.get("session_id"),
            reasoning=kwargs.get("reasoning"),
            disabled_tools=kwargs.get("disabled_tools"),
        )

    def resume_key_from_trajectory(self, trajectory: Trajectory) -> str | None:
        return trajectory.metadata.get("opencode_session_id")

    def resume_session(
        self,
        mcp_servers: list[MCPServer],
        *,
        session_id: str,
        resume_key: str,
        trajectory: Trajectory | None = None,
        **kwargs: Any,
    ) -> OpencodeSession:
        session = OpencodeSession(
            mcp_servers=mcp_servers,
            model=kwargs.get("model") or (trajectory.model if trajectory else None),
            system_prompt=(trajectory.system_prompt if trajectory else None) or None,
            session_id=session_id,
            reasoning=(trajectory.reasoning if trajectory else None) or None,
            disabled_tools=kwargs.get("disabled_tools"),
        )
        # The opencode CLI resumes via `--session <opencode_session_id>`.
        session._opencode_session_id = resume_key
        session._has_sent = True
        if trajectory is not None:
            session._restore_from_trajectory(trajectory)
        return session

    def start_interactive(self, initial_prompt, mcp_servers, capture_bytes=0, **kwargs):  # type: ignore[override]
        """Launch ``opencode`` interactively (TUI) with an initial prompt.

        opencode's interactive mode uses a full split-footer TUI. We launch it
        through a pty so stdin/stdout/stderr are inherited and a copy of stdout
        is captured.
        """
        from caw._pty import drive_interactive_pty

        cmd = [_find_opencode_binary(), "run", "--interactive"]

        model = kwargs.get("model")
        if model:
            cmd += ["--model", model]
        reasoning = kwargs.get("reasoning")
        if reasoning:
            cmd += ["--variant", reasoning]
        if kwargs.get("dangerously_skip_permissions", True):
            cmd += ["--dangerously-skip-permissions"]

        config_path: str | None = None
        disabled_tools = kwargs.get("disabled_tools")
        if mcp_servers or disabled_tools:
            config: dict[str, Any] = {"$schema": "https://opencode.ai/config.json"}
            if mcp_servers:
                mcp: dict[str, Any] = {}
                for srv in mcp_servers:
                    if srv.url:
                        entry: dict[str, Any] = {"type": "remote", "url": srv.url}
                    else:
                        entry = {"type": "local", "command": [srv.command] + list(srv.args or [])}
                        if srv.env:
                            entry["environment"] = srv.env
                    mcp[srv.name] = entry
                config["mcp"] = mcp
            if disabled_tools:
                config["tools"] = {name: False for name in disabled_tools}
            fd, config_path = tempfile.mkstemp(suffix=".json", prefix="caw_opencode_")
            with os.fdopen(fd, "w") as f:
                json.dump(config, f)

        cmd.append(initial_prompt)

        env = {"OPENCODE_CONFIG": config_path} if config_path else None

        def _cleanup() -> None:
            if config_path and os.path.exists(config_path):
                try:
                    os.unlink(config_path)
                except OSError:
                    pass

        return drive_interactive_pty(cmd, env=env, capture_bytes=capture_bytes, on_exit=_cleanup)
