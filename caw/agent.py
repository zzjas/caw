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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from caw.display import get_global_display
from caw.logger import AgentLogger
from caw.models import AgentSpec, InteractiveResult, MCPServer, ModelTier, ToolGroup, ToolUse, Trajectory, Turn
from caw.provider import Provider, ProviderSession
from caw.storage import SessionStore
from caw.toolkit import ToolKit
from caw.mcp import create_stateless_tool_server

if TYPE_CHECKING:
    from caw.health import ProviderHealth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variable overrides
#
# These let you configure caw globally without changing code.  Each one is
# used as a fallback when the corresponding value is not set explicitly via
# the Agent() constructor or method calls.
#
#   CAW_PROVIDER  — Provider backend ("claude_code", "codex", …)
#   CAW_MODEL     — Model name passed to the provider (e.g. "opus", "gpt-5.5")
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
    # CAW_PROVIDER may carry a comma-separated fallback order; take the first.
    provider_name = provider_name.split(",")[0].strip()
    if provider_name not in _PROVIDER_REGISTRY:
        available = list(_PROVIDER_REGISTRY.keys())
        raise ValueError(f"Unknown provider {provider_name!r}. Available: {available}")
    return _PROVIDER_REGISTRY[provider_name]()


# Global provider fallback order, used by ``provider="auto"`` / unset provider.
_PROVIDER_ORDER: list[str] | None = None
# Optional per-provider model selection that rides along with the order, keyed
# by provider name.  Values are concrete model strings or ``ModelTier`` values.
_PROVIDER_ORDER_MODELS: dict[str, str | ModelTier] = {}


def set_provider_order(
    order: list[str | tuple[str, str | ModelTier]] | None,
    *,
    models: dict[str, str | ModelTier] | None = None,
) -> None:
    """Set the global provider fallback order, optionally with per-provider models.

    Pass provider names/aliases in priority order, e.g.
    ``set_provider_order(["claude", "codex", "opencode"])``.  Agents created
    with ``provider="auto"`` (or with no ``provider`` and no ``CAW_PROVIDER``)
    will select the first *installed* provider in this order and gracefully
    fall back to the next on a first-send failure.  Pass ``None`` to clear it.

    To also pin a model per provider, give ``(name, model)`` tuples and/or a
    ``models`` mapping; both accept a concrete model string or a `ModelTier`::

        set_provider_order([("claude", ModelTier.STRONGEST), ("codex", "gpt-5.5")])
        set_provider_order(["claude", "codex"], models={"claude": "opus"})

    A provider's order-model is applied only when the Agent itself sets no
    ``model`` — an explicit ``model=`` (or ``CAW_MODEL``) on the Agent wins.
    Because the model is attached to a specific provider, it is honored even
    when that provider is reached as a fallback (unlike a bare Agent-level model
    string, which is provider-specific and dropped on fallback).

    An explicit ``provider=`` argument on an Agent always overrides this order.
    """
    global _PROVIDER_ORDER, _PROVIDER_ORDER_MODELS
    if not order:
        _PROVIDER_ORDER = None
        _PROVIDER_ORDER_MODELS = {}
        return
    names: list[str] = []
    order_models: dict[str, str | ModelTier] = {}
    for item in order:
        if isinstance(item, (tuple, list)):
            name = str(item[0])
            model = item[1] if len(item) > 1 else None
            if model is not None:
                order_models[name] = model
        else:
            name = str(item)
        names.append(name)
    if models:
        for name, model in models.items():
            if model is not None:
                order_models[str(name)] = model
    _PROVIDER_ORDER = names
    _PROVIDER_ORDER_MODELS = order_models


def get_provider_order() -> list[str] | None:
    """Return the global provider fallback order, or ``None`` if unset."""
    return list(_PROVIDER_ORDER) if _PROVIDER_ORDER else None


def get_provider_models() -> dict[str, str | ModelTier]:
    """Return the per-provider model overrides set via `set_provider_order`."""
    return dict(_PROVIDER_ORDER_MODELS)


def _provider_order_model(provider_name: str | None) -> str | ModelTier | None:
    """Look up the order-model for *provider_name* (None if unset)."""
    if not provider_name:
        return None
    return _PROVIDER_ORDER_MODELS.get(provider_name)


def _resolve_provider_order(provider: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Resolve an Agent's ``provider`` argument into an ordered list of names.

    * a list/tuple → the explicit fallback order
    * a single name string (other than ``"auto"``) → pinned, ``[name]``
    * ``None`` or ``"auto"`` → the global order, else ``CAW_PROVIDER`` (which
      may be a comma-separated list), else the default provider
    """
    if isinstance(provider, (list, tuple)):
        order = [str(p) for p in provider]
    elif isinstance(provider, str) and provider != "auto":
        order = [provider]
    elif _PROVIDER_ORDER:
        order = list(_PROVIDER_ORDER)
    else:
        env = os.environ.get(CAW_PROVIDER, "").strip()
        order = [p.strip() for p in env.split(",") if p.strip()] if env else [DEFAULT_PROVIDER]
    if not order:
        order = [DEFAULT_PROVIDER]
    for name in order:
        if name not in _PROVIDER_REGISTRY:
            raise ValueError(f"Unknown provider {name!r}. Available: {list(_PROVIDER_REGISTRY.keys())}")
    return order


def _select_installed(order: list[str]) -> tuple[int, Provider]:
    """Return ``(index, provider)`` for the first provider in *order* whose CLI
    is installed (a fast, no-network check).  Falls back to the first entry
    when none are installed, so the eventual send raises a friendly error."""
    for i, name in enumerate(order):
        prov = _resolve_provider(name)
        if prov.find_binary() is not None:
            return i, prov
    return 0, _resolve_provider(order[0])


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
    """Parse a JSON handle produced by `_encode_resume_handle`.

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
        fallback_build: Callable[[], ProviderSession | None] | None = None,
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
        # Auto-provider fallback: builds the next provider's session, or returns
        # None when the order is exhausted.  Consulted only on the first send.
        self._fallback_build = fallback_build
        self._committed = False
        if logger is not None:
            self._session.set_logger(logger)

    async def send_async(self, message: str) -> Turn:
        """Async version of `send` — runs in a thread.

        Messages are processed in FIFO order: if multiple ``send_async``
        calls overlap, each waits for the previous one to finish before
        starting.  This lets you fire-and-forget multiple messages:

            tasks = [asyncio.create_task(session.send_async(m)) for m in msgs]
            turns = await asyncio.gather(*tasks)  # executed in order

        You can also do async work while a send is in progress:

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

        When the session was created with an auto-provider fallback order, the
        *first* send transparently moves to the next provider if the current
        one fails (CLI missing, auth error) or is rate-limited — the caller
        never sees the exception.  Once a provider has produced the first turn
        the session is committed to it (conversation context cannot be moved
        across CLIs), so later failures propagate normally.
        """
        if self._readonly:
            raise RuntimeError("Cannot send messages on a loaded session")
        with self._send_lock:
            if not self._committed and self._fallback_build is not None:
                return self._first_send_with_fallback(message)
            return self._send_with_autowait(message)

    def _raw_turn(self, message: str) -> Turn:
        """Run one send against the current provider session (no persistence).

        Sets up the live-snapshot step callback and attaches subagent
        trajectories.  Does *not* append to the store — callers persist only
        once a turn is final (so abandoned fallback attempts leave no record).
        """

        def _save_step(blocks, _msg=message):
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
        try:
            turn = self._session.send(message)
        finally:
            self._session.set_step_callback(None)

        _attach_subagent_trajectories(turn, self._subagent_traj_dir)
        return turn

    def _persist_turn(self, turn: Turn) -> None:
        if self._store is not None:
            self._store.append_turn(turn, self.trajectory, raw_output=self._session.last_raw_output)

    def _wait_for_limit(self, wait_minutes: int) -> None:
        logger.warning("Usage limit reached. Auto-waiting %s min before resuming.", wait_minutes)
        display = get_global_display()
        if display:
            display.on_metadata(auto_wait=f"sleeping {wait_minutes}min until limit resets")
        time.sleep(wait_minutes * 60)

    def _send_with_autowait(self, message: str) -> Turn:
        """Normal send path: persist each turn, auto-wait on usage limits."""
        current_message = message
        while True:
            turn = self._raw_turn(current_message)
            self._persist_turn(turn)
            wait = self._session.detect_usage_limit(turn) if self._auto_wait else None
            if wait is not None:
                self._wait_for_limit(wait)
                current_message = _AUTO_WAIT_RESUME_MESSAGE
                continue
            return turn

    def _swap_session(self, new_session: ProviderSession) -> None:
        self._session = new_session
        if self._logger is not None:
            self._session.set_logger(self._logger)

    def _first_send_with_fallback(self, message: str) -> Turn:
        """First send across an auto-provider order: try each until one yields a
        clean turn, then commit.  Raises only when every provider is exhausted."""
        while True:
            try:
                turn = self._raw_turn(message)
            except Exception:
                nxt = self._fallback_build() if self._fallback_build else None
                if nxt is None:
                    raise  # no provider left to try
                self._swap_session(nxt)
                continue

            # A usage-limited first turn → prefer switching providers over waiting.
            wait = self._session.detect_usage_limit(turn)
            if wait is not None:
                nxt = self._fallback_build() if self._fallback_build else None
                if nxt is not None:
                    self._swap_session(nxt)
                    continue
                # Order exhausted: honour auto-wait on the last provider.
                self._committed = True
                self._persist_turn(turn)
                if self._auto_wait:
                    self._wait_for_limit(wait)
                    return self._send_with_autowait(_AUTO_WAIT_RESUME_MESSAGE)
                return turn

            self._committed = True
            self._persist_turn(turn)
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
        session._traj_path = None
        session._logger = None
        session._fallback_build = None
        session._committed = True
        session._loaded_trajectory = traj
        return session

    @property
    def resume_handle(self) -> str:
        """Opaque string for resuming this session later, possibly in another
        process.

        Store it anywhere (a database, a file, …) and pass it to
        `Agent.resume_session`.  The handle is self-contained: it carries
        the backend resume key, so resuming works even without the original
        ``data_dir`` (you just won't get the prior trajectory restored).  If
        the resuming `Agent` *does* share the same ``data_dir``, the
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

    The ``provider`` argument may be:

    * a single name (e.g. ``"claude"``) — pinned to that provider;
    * a list (e.g. ``["claude", "codex", "opencode"]``) — a fallback order;
    * ``"auto"`` (or omitted) — use the global order from
      `caw.set_provider_order`, else ``CAW_PROVIDER`` (which may be a
      comma-separated list), else the default provider.

    With a fallback order, the agent selects the first *installed* provider and,
    on the first send, transparently moves to the next provider if that one
    fails or is rate-limited — no exception handling needed by the caller.
    """

    def __init__(
        self,
        provider: str | list[str] | None = None,
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
        """The resolved provider instance (lazily created).

        For an auto/fallback order, this is the first provider in the order
        whose CLI is installed (a fast, no-network check)."""
        if self._provider is None:
            _, self._provider = _select_installed(self._resolved_order())
        return self._provider

    def _resolved_order(self) -> list[str]:
        """The provider fallback order for this agent (see `_resolve_provider_order`)."""
        return _resolve_provider_order(self._provider_name)

    def _is_pinned_single(self) -> bool:
        """True when the agent is pinned to one explicit provider name."""
        return isinstance(self._provider_name, str) and self._provider_name != "auto"

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

        If *handle* is a `ToolKit` instance, ``as_server()``
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

    def check_health(self, live: bool = False) -> "ProviderHealth":
        """Report raw health signals for this agent's provider.

        Fast by default (CLI installed + credential introspection, no token
        cost).  Pass ``live=True`` to additionally probe whether the provider
        responds and is currently rate-limited.  See
        `caw.health.ProviderHealth`.
        """
        model = self._kwargs.get("model")
        if isinstance(model, ModelTier):
            model = self.provider.resolve_model(model)
        return self.provider.check_health(live=live, model=model)

    def interactive(self, initial_prompt: str, capture_bytes: int = 0, **kwargs: Any) -> InteractiveResult:
        """Launch the provider binary interactively with an initial prompt.

        The user interacts with the agent directly in their terminal.
        A copy of stdout is captured via a pty.  MCP tool servers are
        started before launch and stopped after the process exits.

        Args:
            initial_prompt: The first message sent to the agent.
            capture_bytes: Maximum bytes of terminal output to keep (tail).
                ``0`` (default) means capture everything.

        Returns:
            An ``InteractiveResult`` with the exit code and captured terminal
            output.
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
        to maintain a multi-turn session:

            traj = agent.completion("Explain this code")
            print(traj.result)
        """
        session = self.start_session(**kwargs)
        session.send(message)
        return session.end()

    def start_session(self, traj_path: str | Path | None = None, **kwargs: Any) -> Session:
        """Start a new interactive session with the agent.

        Args:
            traj_path: If set, the trajectory is saved to this path after each
                step and when ``Session.end`` is called.

        Additional keyword arguments are forwarded to the session. Passing a
        ``logger`` (any object with ``info``/``warn``/``error`` string methods)
        makes every major event — user message, tool call, tool result,
        assistant text, thinking, turn-end stats — also emit a one-line summary
        through it, in addition to the console ``Display``. See ``caw.logger``.
        """
        base = {**self._kwargs, **kwargs}
        auto_wait, session_metadata, logger = self._pop_session_opts(base)

        # Generate session_id early so the JSONL path is known before MCP configs
        session_id: str | None = None
        store: SessionStore | None = None
        if self._data_dir:
            session_id = str(uuid_mod.uuid4())
            store = SessionStore(self._data_dir, session_id)

        subagent_traj_dir, all_handles, all_mcp = self._start_tool_servers(store)

        # Resolve the fallback order and select the first installed provider.
        order = self._resolved_order()
        sel_index, _ = _select_installed(order)
        chosen = order[sel_index:]
        first_choice = order[0]

        def _build(provider_name: str) -> ProviderSession:
            prov = _resolve_provider(provider_name)
            merged = self._provider_session_kwargs(
                prov, base, is_fallback=(provider_name != first_choice), provider_name=provider_name
            )
            if session_id:
                merged["session_id"] = session_id
            return prov.start_session(mcp_servers=all_mcp, **merged)

        remaining = list(chosen[1:])

        def _fallback_build() -> ProviderSession | None:
            if not remaining:
                return None
            return _build(remaining.pop(0))

        provider_session = _build(chosen[0])

        session = Session(
            provider_session,
            store=store,
            subagent_traj_dir=subagent_traj_dir,
            tool_handles=all_handles,
            auto_wait=auto_wait,
            metadata=session_metadata,
            logger=logger,
            fallback_build=_fallback_build if remaining else None,
        )

        if traj_path is not None:
            session._traj_path = traj_path

        if store:
            store.write_metadata(session.trajectory)

        return session

    def resume_session(self, resume_handle: str, **kwargs: Any) -> Session:
        """Resume a session from a handle produced by `Session.resume_handle`.

        Returns a live `Session` whose next `Session.send`
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
        resume_provider: Provider | None
        if decoded is not None:
            resume_provider = _resolve_provider(decoded["provider"])
            session_id = decoded["session_id"]
            resume_key: str | None = decoded["resume_key"]
            # A pinned single-provider agent must match the handle; an auto /
            # multi-provider agent simply resumes with the handle's provider.
            if self._is_pinned_single() and resume_provider.name != self.provider.name:
                raise ValueError(
                    f"Handle is for provider {resume_provider.name!r} but this Agent is pinned to {self.provider.name!r}."
                )
        else:
            # Treat the string as a bare caw session id; the resume key (and
            # provider) must then come from the on-disk trajectory.
            session_id = resume_handle
            resume_key = None
            resume_provider = None

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
            if resume_provider is None:
                resume_provider = _resolve_provider(trajectory.agent)
            resume_key = resume_provider.resume_key_from_trajectory(trajectory)
            if not resume_key:
                raise ValueError(f"Cannot resume {resume_handle!r}: no resume key was persisted for this session.")

        assert resume_provider is not None  # set on both branches by this point

        base = {**self._kwargs, **kwargs}
        auto_wait, session_metadata, logger = self._pop_session_opts(base)

        if self._data_dir:
            store = SessionStore(self._data_dir, session_id, resume=True)

        subagent_traj_dir, all_handles, all_mcp = self._start_tool_servers(store)

        merged = self._provider_session_kwargs(resume_provider, base)
        # session_id is fixed by the handle/trajectory, not generated.
        merged.pop("session_id", None)
        provider_session = resume_provider.resume_session(
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
        """Strip Session-only kwargs out of *merged*.

        Returns ``(auto_wait, session_metadata, logger)``.  Model-tier and tool
        resolution are deferred to `_provider_session_kwargs` so they can
        be applied per provider (each maps tiers/tools differently).
        """
        auto_wait = merged.pop("auto_wait", True)
        session_metadata: dict[str, Any] = merged.pop("metadata", {})
        logger: AgentLogger | None = merged.pop("logger", None) or self._logger
        # Agent-level metadata as base, session kwargs override
        if self._metadata:
            session_metadata = {**self._metadata, **session_metadata}
        return auto_wait, session_metadata, logger

    def _provider_session_kwargs(
        self,
        provider: Provider,
        base: dict[str, Any],
        *,
        is_fallback: bool = False,
        provider_name: str | None = None,
    ) -> dict[str, Any]:
        """Resolve provider-specific ``start_session`` kwargs from *base*.

        Maps a `ModelTier` via the provider and translates the ``tools``
        group into the provider's restriction kwargs.  *base* is not mutated.

        A concrete ``model`` *string* is provider-specific, so it is dropped for
        a fallback provider (``is_fallback=True``) — the fallback uses its own
        default rather than a foreign model id.  Pass a `ModelTier` for a
        portable model selection across an auto-provider order.

        When *base* carries no model and a per-provider model was attached via
        `set_provider_order`, that model is applied here.  Because it is bound to
        this specific provider it is never treated as a foreign id, so it
        survives even when *provider* is reached as a fallback.
        """
        merged = dict(base)
        model = merged.get("model")
        from_order = False
        if model is None:
            order_model = _provider_order_model(provider_name)
            if order_model is not None:
                model = order_model
                from_order = True
        if isinstance(model, ModelTier):
            merged["model"] = provider.resolve_model(model)
        elif isinstance(model, str):
            if is_fallback and not from_order:
                merged.pop("model", None)
            else:
                merged["model"] = model
        tools = merged.pop("tools", None)
        if tools is None:
            tools = ToolGroup.ALL - ToolGroup.INTERACTION
        merged.update(provider.resolve_tool_restrictions(tools))
        return merged

    def _start_tool_servers(self, store: SessionStore | None) -> tuple[str | None, list[Any], list[MCPServer]]:
        """Start tool/subagent servers (provider-independent).

        Returns ``(subagent_traj_dir, all_handles, all_mcp)``.  Tool *restriction*
        resolution is provider-specific and handled in
        `_provider_session_kwargs`.
        """
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
