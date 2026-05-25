"""Concrete Agent and Session — the main user-facing API."""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid as uuid_mod
from pathlib import Path
from typing import Any

from caw.display import get_global_display
from caw.logger import AgentLogger
from caw.models import AgentSpec, InteractiveResult, MCPServer, ModelTier, ToolGroup, ToolUse, Trajectory, Turn
from caw.provider import Provider, ProviderSession
from caw.storage import SessionStore
from caw.toolkit import ToolKit
from caw.mcp import create_stateless_tool_server

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variable overrides
#
# These let you configure caw globally without changing code.  Each one is
# used as a fallback when the corresponding value is not set explicitly via
# the Agent() constructor or method calls.
#
#   CAW_PROVIDER  — Provider backend ("claude_code", "codex", …)
#   CAW_MODEL     — Model name passed to the provider (e.g. "gpt-5.2-codex")
#   CAW_EFFORT    — Reasoning effort level (e.g. "high", "medium", "low")
#   CAW_AUTOWAIT  — Auto-wait on usage limit ("1"=on, "0"/"false"=off; default on)
# ---------------------------------------------------------------------------
DEFAULT_PROVIDER = "claude_code"
CAW_PROVIDER = "CAW_PROVIDER"
CAW_MODEL = "CAW_MODEL"
CAW_EFFORT = "CAW_EFFORT"
CAW_AUTOWAIT = "CAW_AUTOWAIT"

_PROVIDER_REGISTRY: dict[str, type[Provider]] = {}

_AUTO_WAIT_RESUME_MESSAGE = "Usage limit reached earlier, now you may continue the work."

# Must match caw.mcp._TRAJ_MARKER_PREFIX / _SUFFIX
_TRAJ_MARKER_RE = re.compile(r"\n<!-- caw_traj:([\w-]+) -->$")


def register_provider(name: str, cls: type[Provider]) -> None:
    """Register a provider class under the given name."""
    _PROVIDER_REGISTRY[name] = cls


def _resolve_provider(name: str | None) -> Provider:
    """Resolve provider: explicit name > env var > default."""
    provider_name = name or os.environ.get(CAW_PROVIDER) or DEFAULT_PROVIDER
    if provider_name not in _PROVIDER_REGISTRY:
        available = list(_PROVIDER_REGISTRY.keys())
        raise ValueError(f"Unknown provider {provider_name!r}. Available: {available}")
    return _PROVIDER_REGISTRY[provider_name]()


def _encode_resume_handle(provider: str, session_id: str, resume_key: str) -> str:
    """Pack everything needed to resume — provider, caw session id, and the
    backend resume key — into a JSON handle string.  Self-contained so a
    session can be resumed in a new process with or without the original
    ``data_dir``."""
    return json.dumps(
        {
            "version": 1,
            "provider": provider,
            "session_id": session_id,
            "resume_key": resume_key,
        }
    )


def _decode_resume_handle(handle: str) -> dict[str, Any] | None:
    """Parse a JSON handle produced by :func:`_encode_resume_handle`.

    Returns the payload dict, or ``None`` if *handle* is not a self-contained
    handle (e.g. a bare session id), so callers can fall back to disk lookup.
    """
    try:
        data = json.loads(handle)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict) and data.get("resume_key"):
        return data
    return None


def _attach_subagent_trajectories(turn: Turn, traj_dir: str | None) -> None:
    """Scan a turn's tool outputs for trajectory markers and attach them.

    For each ToolUse whose output ends with ``<!-- caw_traj:<uuid> -->``:
    1. Load the trajectory JSON from ``traj_dir/<uuid>.json``
    2. Attach it as ``tool_use.subagent_trajectory``
    3. Strip the marker from ``tool_use.output``
    """
    if not traj_dir:
        return
    for block in turn.output:
        if not isinstance(block, ToolUse):
            continue
        m = _TRAJ_MARKER_RE.search(block.output)
        if not m:
            continue
        traj_id = m.group(1)
        traj_path = os.path.join(traj_dir, f"{traj_id}.json")
        try:
            with open(traj_path) as f:
                traj_dict = json.load(f)
            block.subagent_trajectory = Trajectory.from_dict(traj_dict)
        except (OSError, json.JSONDecodeError, KeyError):
            pass  # best-effort
        # Strip marker regardless
        block.output = block.output[: m.start()]


class Session:
    """A live interaction session with a coding agent."""

    def __init__(
        self,
        provider_session: ProviderSession,
        store: SessionStore | None = None,
        subagent_traj_dir: str | None = None,
        tool_handles: list[Any] | None = None,
        auto_wait: bool = True,
        metadata: dict[str, Any] | None = None,
        logger: AgentLogger | None = None,
    ) -> None:
        self._session = provider_session
        self._store = store
        self._subagent_traj_dir = subagent_traj_dir
        self._tool_handles = tool_handles or []
        self._auto_wait = auto_wait
        self._metadata: dict[str, Any] = dict(metadata) if metadata else {}
        self._readonly = False
        self._send_lock = threading.Lock()
        self._async_send_lock: asyncio.Lock | None = None
        self._traj_path: str | Path | None = None
        self._logger = logger
        if logger is not None:
            self._session.set_logger(logger)

    async def send_async(self, message: str) -> Turn:
        """Async version of :meth:`send` — runs in a thread.

        Messages are processed in FIFO order: if multiple ``send_async``
        calls overlap, each waits for the previous one to finish before
        starting.  This lets you fire-and-forget multiple messages::

            tasks = [asyncio.create_task(session.send_async(m)) for m in msgs]
            turns = await asyncio.gather(*tasks)  # executed in order

        You can also do async work while a send is in progress::

            task = asyncio.create_task(session.send_async(prompt))
            while not task.done():
                source = await asyncio.wait_for(queue.get(), timeout=0.5)
                yield source
            turn = await task
        """
        if self._async_send_lock is None:
            self._async_send_lock = asyncio.Lock()
        async with self._async_send_lock:
            return await asyncio.to_thread(self.send, message)

    def send(self, message: str) -> Turn:
        """Send a message and get the agent's response turn.

        When auto-wait is enabled and the provider reports a usage limit,
        this method sleeps until the limit resets and then automatically
        resumes the conversation — transparently to the caller.
        """
        if self._readonly:
            raise RuntimeError("Cannot send messages on a loaded session")
        with self._send_lock:
            current_message = message

            while True:
                # Set up per-step callback so traj_path is updated in real time
                def _save_step(blocks, _msg=current_message):
                    traj = self.trajectory
                    partial_turn = Turn(input=_msg, output=list(blocks))
                    traj.turns.append(partial_turn)
                    if self._traj_path:
                        p = Path(self._traj_path)
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text(json.dumps(traj.to_dict(), indent=2))
                    if self._store:
                        self._store._save_trajectory(traj)

                self._session.set_step_callback(_save_step)
                turn = self._session.send(current_message)
                self._session.set_step_callback(None)

                # Attach subagent trajectories from marker files
                _attach_subagent_trajectories(turn, self._subagent_traj_dir)

                if self._store is not None:
                    self._store.append_turn(turn, self.trajectory, raw_output=self._session.last_raw_output)

                # Ask the provider whether this turn hit a usage limit
                if self._auto_wait:
                    wait_minutes = self._session.detect_usage_limit(turn)
                    if wait_minutes is not None:
                        logger.warning(
                            "Usage limit reached. Auto-waiting %s min before resuming.",
                            wait_minutes,
                        )
                        display = get_global_display()
                        if display:
                            display.on_metadata(
                                auto_wait=f"sleeping {wait_minutes}min until limit resets",
                            )
                        time.sleep(wait_minutes * 60)
                        current_message = _AUTO_WAIT_RESUME_MESSAGE
                        continue

                return turn

    def end(self) -> Trajectory:
        """End the session and return the complete trajectory."""
        if self._readonly:
            raise RuntimeError("Cannot send messages on a loaded session")
        self._session.end()
        traj = self.trajectory
        traj.completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if traj.turns and self._session.detect_usage_limit(traj.turns[-1]) is not None:
            traj.usage_limited = True
        if self._store is not None:
            self._store.finalize(traj)
        # Stop all tool server handles
        for handle in self._tool_handles:
            try:
                handle.stop_sync()
            except Exception:
                pass
        # Auto-save trajectory if configured
        if self._traj_path is not None:
            try:
                p = Path(self._traj_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "w") as f:
                    json.dump(traj.to_dict(), f, indent=2)
            except Exception:
                logger.warning("Failed to save trajectory to %s", self._traj_path, exc_info=True)
        return traj

    @property
    def trajectory(self) -> Trajectory:
        """Accumulated trajectory (available during and after the session)."""
        if self._readonly:
            return self._loaded_trajectory
        traj = self._session.trajectory
        if self._metadata:
            # Session metadata merges on top of provider metadata
            traj.metadata = {**traj.metadata, **self._metadata}
        return traj

    def save_trajectory(self, path: str | Path) -> None:
        """Save the trajectory to a JSON file at the given path."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(self.trajectory.to_dict(), f, indent=2)

    @classmethod
    def load_trajectory(cls, path: str | Path) -> Session:
        """Load a trajectory from a JSON file. The returned session is read-only."""
        with open(path) as f:
            data = json.load(f)
        traj = Trajectory.from_dict(data)
        return cls._from_trajectory(traj)

    @classmethod
    def _from_trajectory(cls, traj: Trajectory) -> Session:
        session = object.__new__(cls)
        session._session = None
        session._store = None
        session._subagent_traj_dir = None
        session._tool_handles = []
        session._auto_wait = False
        session._metadata = {}
        session._readonly = True
        session._send_lock = threading.Lock()
        session._async_send_lock = None
        session._loaded_trajectory = traj
        return session

    @property
    def resume_handle(self) -> str:
        """Opaque string for resuming this session later, possibly in another
        process.

        Store it anywhere (a database, a file, …) and pass it to
        :meth:`Agent.resume_session`.  The handle is self-contained: it carries
        the backend resume key, so resuming works even without the original
        ``data_dir`` (you just won't get the prior trajectory restored).  If
        the resuming :class:`Agent` *does* share the same ``data_dir``, the
        full history is restored and new turns are appended.

        Raises if the backend has not yet assigned a resume key — send at least
        one message first.
        """
        traj = self.trajectory
        if not self._readonly and self._session is not None:
            resume_key = self._session.resume_key
        else:
            resume_key = _resolve_provider(traj.agent).resume_key_from_trajectory(traj)
        if not resume_key:
            raise RuntimeError(
                "No resume key available yet — send at least one message before requesting a resume handle."
            )
        return _encode_resume_handle(traj.agent, traj.session_id, resume_key)

    @property
    def session_dir(self) -> Path | None:
        """Path to the session's data directory, or None if persistence is disabled."""
        if self._store is not None:
            return self._store.session_dir
        return None

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *args: Any) -> None:
        self.end()


class Agent:
    """Coding agent wrapper — unified interface across providers.

    Provider resolution order:
    1. Explicit ``provider`` argument
    2. ``CAW_PROVIDER`` environment variable
    3. Default provider
    """

    def __init__(
        self,
        provider: str | None = None,
        data_dir: str | None = None,
        system_prompt: str | None = None,
        model: str | ModelTier | None = None,
        reasoning: str | None = None,
        tools: ToolGroup | None = None,
        tool_servers: list[Any] | None = None,
        stateless_tools: list[Any] | None = None,
        name: str = "",
        description: str = "",
        logger: AgentLogger | None = None,
        **kwargs: Any,
    ) -> None:
        self._provider_name = provider
        self._provider: Provider | None = None
        self._mcp_servers: list[MCPServer] = []
        self._subagents: list[AgentSpec] = []
        self._logger = logger
        self._tool_servers: list[Any] = []  # list[MCPServerHandle], lazy import
        if tool_servers:
            for ts in tool_servers:
                self.add_tool_server(ts)
        if stateless_tools:
            self._tool_servers.append(create_stateless_tool_server(stateless_tools))
        self._data_dir = data_dir
        self._name = name
        self._description = description
        self._metadata: dict[str, Any] = {}
        if tools is not None:
            kwargs["tools"] = tools
        if system_prompt is not None:
            kwargs["system_prompt"] = system_prompt
        if model is not None:
            kwargs["model"] = model
        elif os.environ.get(CAW_MODEL):
            kwargs["model"] = os.environ[CAW_MODEL]
        if reasoning is not None:
            kwargs["reasoning"] = reasoning
        elif os.environ.get(CAW_EFFORT):
            kwargs["reasoning"] = os.environ[CAW_EFFORT]
        if "auto_wait" not in kwargs:
            env_val = os.environ.get(CAW_AUTOWAIT, "").strip().lower()
            if env_val in ("0", "false", "no", "off"):
                kwargs["auto_wait"] = False
            # Otherwise leave unset so provider default (True) applies
        self._kwargs = kwargs

    def set_provider(self, provider: str) -> None:
        """Set or change the provider before starting a session."""
        self._provider_name = provider
        self._provider = None

    @property
    def provider(self) -> Provider:
        """The resolved provider instance (lazily created)."""
        if self._provider is None:
            self._provider = _resolve_provider(self._provider_name)
        return self._provider

    @property
    def mcp_servers(self) -> list[MCPServer]:
        """Currently configured MCP servers."""
        return list(self._mcp_servers)

    @property
    def metadata(self) -> dict[str, Any]:
        """Mutable metadata dict carried onto every session's trajectory."""
        return self._metadata

    def add_mcp_server(self, server: MCPServer) -> None:
        """Register an MCP server for tool access."""
        self._mcp_servers.append(server)

    def add_tool_server(self, handle: Any) -> None:
        """Register a custom HTTP tool server (MCPServerHandle or ToolKit).

        If *handle* is a :class:`~caw.toolkit.ToolKit` instance, ``as_server()``
        is called automatically.  The handle's lifecycle (start/stop) is managed
        by the session.
        """
        if isinstance(handle, ToolKit):
            handle = handle.as_server()
        self._tool_servers.append(handle)

    def set_model(self, model: str | ModelTier) -> None:
        """Set the model to use for sessions."""
        self._kwargs["model"] = model

    def set_reasoning(self, reasoning: str) -> None:
        """Set the reasoning budget token (e.g. ``'medium'``)."""
        self._kwargs["reasoning"] = reasoning

    def set_system_prompt(self, system_prompt: str) -> None:
        """Set a system prompt that guides the agent's behavior for the session."""
        self._kwargs["system_prompt"] = system_prompt

    def set_tools(self, tools: ToolGroup) -> None:
        """Set the tool permission groups for sessions."""
        self._kwargs["tools"] = tools

    def add_subagent(self, spec: AgentSpec) -> None:
        """Register a subagent that will be exposed as a tool."""
        self._subagents.append(spec)

    def get_spec(self) -> AgentSpec:
        """Return an AgentSpec snapshot of this agent's current configuration."""
        return AgentSpec(
            name=self._name,
            description=self._description,
            system_prompt=self._kwargs.get("system_prompt", ""),
            model=self._kwargs.get("model", ""),
            reasoning=self._kwargs.get("reasoning", ""),
            tools=self._kwargs.get("tools"),
            tool_servers=list(self._tool_servers),
            mcp_servers=list(self._mcp_servers),
            subagents=list(self._subagents),
            metadata=dict(self._metadata),
        )

    def _subagent_tool_servers(self, traj_dir: str, jsonl_path: str | None = None) -> list[Any]:
        """Convert registered subagents into HTTP tool server handles."""
        from caw.mcp import create_subagent_tool_server

        handles = []
        for spec in self._subagents:
            handle = create_subagent_tool_server(spec, traj_dir, jsonl_path)
            handles.append(handle)
        return handles

    def check_limit(self) -> int | None:
        """Check if the provider's usage limit is currently active.

        Sends a minimal test prompt to detect whether the configured
        provider and model are currently rate-limited.  Returns the
        estimated number of minutes until the limit resets, or ``None``
        if no limit is detected.

        This incurs a small token cost for the probe request.
        """
        model = self._kwargs.get("model")
        if isinstance(model, ModelTier):
            model = self.provider.resolve_model(model)
        return self.provider.check_limit(model=model)

    def interactive(self, initial_prompt: str, capture_bytes: int = 0, **kwargs: Any) -> InteractiveResult:
        """Launch the provider binary interactively with an initial prompt.

        The user interacts with the agent directly in their terminal.
        A copy of stdout is captured via a pty.  MCP tool servers are
        started before launch and stopped after the process exits.

        Parameters
        ----------
        initial_prompt:
            The first message sent to the agent.
        capture_bytes:
            Maximum bytes of terminal output to keep (tail).
            ``0`` (default) means capture everything.

        Returns an :class:`InteractiveResult` with the exit code and
        captured terminal output.
        """
        merged = {**self._kwargs, **kwargs}

        # Remove session-only concerns
        merged.pop("auto_wait", None)
        merged.pop("metadata", None)

        # Resolve model tier
        model = merged.get("model")
        if isinstance(model, ModelTier):
            merged["model"] = self.provider.resolve_model(model)

        # Resolve tool restrictions — default to ALL (user is present)
        tools = merged.pop("tools", None)
        if tools is not None:
            restrictions = self.provider.resolve_tool_restrictions(tools)
            merged.update(restrictions)

        # Start MCP tool server handles
        all_handles: list[Any] = list(self._tool_servers)
        for handle in all_handles:
            handle.start_sync()

        all_mcp = list(self._mcp_servers)
        for handle in all_handles:
            all_mcp.append(MCPServer(name=handle.server_id, url=handle.url))

        try:
            return self.provider.start_interactive(
                initial_prompt, mcp_servers=all_mcp, capture_bytes=capture_bytes, **merged
            )
        finally:
            for handle in all_handles:
                try:
                    handle.stop_sync()
                except Exception:
                    pass

    def completion(self, message: str, **kwargs: Any) -> Trajectory:
        """Send a single message and return the complete trajectory.

        Convenience wrapper for simple use cases where you don't need
        to maintain a multi-turn session::

            traj = agent.completion("Explain this code")
            print(traj.result)
        """
        session = self.start_session(**kwargs)
        session.send(message)
        return session.end()

    def start_session(self, traj_path: str | Path | None = None, **kwargs: Any) -> Session:
        """Start a new interactive session with the agent.

        Parameters
        ----------
        traj_path:
            If set, the trajectory is saved to this path after each
            step and when :meth:`Session.end` is called.
        logger:
            Optional generic logger (any object with ``info``/``warn``/
            ``error`` string methods). If set, every major event — user
            message, tool call, tool result, assistant text, thinking,
            turn-end stats — is also emitted as a one-line summary
            through it. See :mod:`caw.logger`.
        """
        merged = {**self._kwargs, **kwargs}
        auto_wait, session_metadata, logger = self._pop_session_opts(merged)

        # Generate session_id early so the JSONL path is known before MCP configs
        session_id: str | None = None
        store: SessionStore | None = None
        if self._data_dir:
            session_id = str(uuid_mod.uuid4())
            store = SessionStore(self._data_dir, session_id)

        subagent_traj_dir, all_handles, all_mcp = self._build_tool_context(merged, store)

        # Pass our session_id so the provider uses it (instead of generating its own)
        if session_id:
            merged["session_id"] = session_id

        provider_session = self.provider.start_session(mcp_servers=all_mcp, **merged)

        session = Session(
            provider_session,
            store=store,
            subagent_traj_dir=subagent_traj_dir,
            tool_handles=all_handles,
            auto_wait=auto_wait,
            metadata=session_metadata,
            logger=logger,
        )

        if traj_path is not None:
            session._traj_path = traj_path

        if store:
            store.write_metadata(session.trajectory)

        return session

    def resume_session(self, resume_handle: str, **kwargs: Any) -> Session:
        """Resume a session from a handle produced by :attr:`Session.resume_handle`.

        Returns a live :class:`Session` whose next :meth:`Session.send`
        continues the original conversation.

        - **Without ``data_dir`` (or if the session isn't on disk):** the backend
          conversation is resumed using the key embedded in the handle, but
          caw's trajectory starts empty (no prior turns restored).
        - **With the original ``data_dir``:** the full trajectory is restored
          and new turns are appended to the original session directory.

        A bare session id is also accepted in place of a full handle, but only
        when ``data_dir`` is set (the resume key is then read from disk).
        """
        decoded = _decode_resume_handle(resume_handle)
        if decoded is not None:
            provider_name = decoded["provider"]
            session_id = decoded["session_id"]
            resume_key: str | None = decoded["resume_key"]
            if provider_name != self.provider.name:
                raise ValueError(
                    f"Handle is for provider {provider_name!r} but this Agent uses {self.provider.name!r}."
                )
        else:
            # Treat the string as a bare caw session id; the resume key must
            # then come from the on-disk trajectory.
            session_id = resume_handle
            resume_key = None

        # Load the persisted trajectory if a data_dir is available.
        trajectory: Trajectory | None = None
        store: SessionStore | None = None
        had_existing = False
        if self._data_dir:
            traj_path = Path(self._data_dir) / "sessions" / session_id / "trajectory.json"
            if traj_path.exists():
                with open(traj_path) as f:
                    trajectory = Trajectory.from_dict(json.load(f))
                had_existing = True

        if resume_key is None:
            if trajectory is None:
                raise FileNotFoundError(
                    f"Cannot resume {resume_handle!r}: it is not a self-contained "
                    f"handle and no persisted session was found"
                    + (f" under {self._data_dir}" if self._data_dir else " (no data_dir set)")
                    + "."
                )
            resume_key = self.provider.resume_key_from_trajectory(trajectory)
            if not resume_key:
                raise ValueError(f"Cannot resume {resume_handle!r}: no resume key was persisted for this session.")

        merged = {**self._kwargs, **kwargs}
        auto_wait, session_metadata, logger = self._pop_session_opts(merged)

        if self._data_dir:
            store = SessionStore(self._data_dir, session_id, resume=True)

        subagent_traj_dir, all_handles, all_mcp = self._build_tool_context(merged, store)

        # session_id is fixed by the handle/trajectory, not generated.
        merged.pop("session_id", None)
        provider_session = self.provider.resume_session(
            mcp_servers=all_mcp,
            session_id=session_id,
            resume_key=resume_key,
            trajectory=trajectory,
            **merged,
        )

        session = Session(
            provider_session,
            store=store,
            subagent_traj_dir=subagent_traj_dir,
            tool_handles=all_handles,
            auto_wait=auto_wait,
            metadata=session_metadata,
            logger=logger,
        )
        # Fresh on-disk session (data_dir set but nothing persisted yet): seed
        # the metadata line like start_session does.
        if store is not None and not had_existing:
            store.write_metadata(session.trajectory)
        return session

    def _pop_session_opts(self, merged: dict[str, Any]) -> tuple[bool, dict[str, Any], AgentLogger | None]:
        """Strip Session-only kwargs out of *merged* and resolve a model tier.

        Returns ``(auto_wait, session_metadata, logger)``; *merged* is left
        holding only provider ``start_session``/``resume_session`` kwargs.
        """
        auto_wait = merged.pop("auto_wait", True)
        session_metadata: dict[str, Any] = merged.pop("metadata", {})
        logger: AgentLogger | None = merged.pop("logger", None) or self._logger
        # Agent-level metadata as base, session kwargs override
        if self._metadata:
            session_metadata = {**self._metadata, **session_metadata}

        # Resolve model tier to concrete model string
        model = merged.get("model")
        if isinstance(model, ModelTier):
            merged["model"] = self.provider.resolve_model(model)
        return auto_wait, session_metadata, logger

    def _build_tool_context(
        self, merged: dict[str, Any], store: SessionStore | None
    ) -> tuple[str | None, list[Any], list[MCPServer]]:
        """Resolve tool restrictions and start tool/subagent servers.

        Consumes ``tools`` from *merged* (applying the default), updates
        *merged* with provider-specific tool restriction kwargs, and returns
        ``(subagent_traj_dir, all_handles, all_mcp)``.
        """
        # Resolve tool restrictions: default to ALL - INTERACTION for automated pipelines
        tools = merged.pop("tools", None)
        if tools is None:
            tools = ToolGroup.ALL - ToolGroup.INTERACTION
        restrictions = self.provider.resolve_tool_restrictions(tools)
        merged.update(restrictions)

        # Create temp dir for subagent trajectory files (if subagents exist)
        subagent_traj_dir: str | None = None
        if self._subagents:
            subagent_traj_dir = tempfile.mkdtemp(prefix="caw_subagent_traj_")

        # Collect all tool server handles (user-registered + subagent)
        all_handles: list[Any] = list(self._tool_servers)
        if self._subagents:
            all_handles += self._subagent_tool_servers(
                subagent_traj_dir,  # type: ignore[arg-type]
                jsonl_path=str(store.jsonl_path) if store else None,
            )

        # Start all HTTP tool servers and collect their MCPServer configs
        for handle in all_handles:
            handle.start_sync()

        all_mcp = list(self._mcp_servers)
        for handle in all_handles:
            all_mcp.append(MCPServer(name=handle.server_id, url=handle.url))

        return subagent_traj_dir, all_handles, all_mcp
