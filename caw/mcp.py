"""FastMCP-based HTTP tool-server infrastructure for CAW.

Ported from ``poc_playground/poc/utils/mcp.py`` with additions for:
- Synchronous start/stop bridge (CAW's Agent API is sync, FastMCP/Uvicorn are async)
- Subagent tool factory (replaces the old stdio ``subagent_server.py``)

Trajectory marker constants must stay in sync with ``caw.agent._TRAJ_MARKER_RE``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import threading
import uuid as uuid_mod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import uvicorn

# FastMCP ships inside the `mcp` package up to 1.x. mcp 2.0 removed
# `mcp.server.fastmcp` outright (its server lives under `mcp.server.mcpserver`
# now), so an environment that resolved `mcp` to 2.x made `import caw` fail
# with a bare ModuleNotFoundError — for every user, including the many who
# never touch a tool server. The dependency pin in pyproject.toml is the real
# fix; this stops an already-broken environment from taking the whole library
# down with it, and turns the failure into a sentence that says what to do.
try:
    from mcp.server.fastmcp import Context, FastMCP

    _FASTMCP_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - depends on the installed mcp
    _FASTMCP_IMPORT_ERROR = exc

    class Context:  # type: ignore[no-redef]
        """Stand-in so ``import caw`` works without a usable FastMCP.

        Only ever used as a type annotation; everything that actually talks to
        MCP goes through `_require_fastmcp` first.
        """

    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _require_fastmcp()


def _require_fastmcp() -> None:
    """Raise a useful error if FastMCP could not be imported."""
    if _FASTMCP_IMPORT_ERROR is None:
        return
    raise RuntimeError(
        "caw's tool servers need FastMCP, which ships inside the `mcp` package "
        "up to 1.x. The installed `mcp` has no `mcp.server.fastmcp` — mcp 2.0 "
        "removed it. Install a compatible one:\n"
        "    pip install 'mcp>=1.0,<2'\n"
        f"(import error: {_FASTMCP_IMPORT_ERROR})"
    ) from _FASTMCP_IMPORT_ERROR


__all__ = [
    "Context",
    "MCPServerHandle",
    "create_mcp_http_server_bundle",
    "create_subagent_tool_server",
    "get_state_from_context",
    "mcp_tool",
    "register_tool",
]

logging.getLogger("mcp.server.streamable_http_manager").setLevel(logging.WARNING)
logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)

# -- Trajectory markers (shared with caw.agent) --------------------------------

_TRAJ_MARKER_PREFIX = "\n<!-- caw_traj:"
_TRAJ_MARKER_SUFFIX = " -->"


# -- Helpers -------------------------------------------------------------------


async def _wait_for_server_ready(host: str, port: int, timeout: float = 5.0) -> None:
    """Poll until the HTTP endpoint is listening."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            if loop.time() >= deadline:
                raise RuntimeError(f"MCP server {host}:{port} did not start in time.") from None
            await asyncio.sleep(0.1)
            continue
        writer.close()
        await writer.wait_closed()
        return


def _create_bound_socket(host: str) -> socket.socket:
    """Create and return a bound socket, keeping it open to reserve the port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock


# -- Decorator / registration --------------------------------------------------


def mcp_tool(
    name: str | None = None,
    *,
    title: str | None = None,
    description: str | None = None,
    annotations: Any | None = None,
    icons: list[Any] | None = None,
    meta: dict[str, Any] | None = None,
    structured_output: bool | None = None,
):
    """Decorator to attach MCP metadata to a tool function."""

    def decorator(func: Callable[..., Any]):
        info = {
            "name": name,
            "title": title,
            "description": description or func.__doc__ or "",
            "annotations": annotations,
            "icons": icons,
            "meta": meta,
            "structured_output": structured_output,
        }
        setattr(func, "_mcp_tool_info", info)
        return func

    return decorator


def register_tool(server: FastMCP, func: Callable[..., Any]) -> None:
    """Register a decorated tool function with a FastMCP server."""
    info = getattr(func, "_mcp_tool_info", None) or getattr(func, "_toolkit_tool_info", {})
    server.tool(
        name=info.get("name"),
        title=info.get("title"),
        description=info.get("description", func.__doc__ or ""),
        annotations=info.get("annotations"),
        icons=info.get("icons"),
        structured_output=info.get("structured_output"),
    )(func)


def get_state_from_context(ctx: Context) -> Any:
    """Return the lifespan state object from a tool Context."""
    return ctx.request_context.lifespan_context


# -- MCPServerHandle -----------------------------------------------------------


@dataclass
class MCPServerHandle:
    """Convenience wrapper exposing runner + agent config for a FastMCP server."""

    server_id: str
    server: FastMCP
    host: str = "127.0.0.1"
    port: int | None = None
    path: str | None = None
    _state_instance: Any = None
    _server_task: asyncio.Task | None = None
    _uvicorn_server: uvicorn.Server | None = None
    uvicorn_log_level: str | None = "error"
    _bound_socket: socket.socket | None = None

    # -- Sync bridge fields (set by start_sync / stop_sync) --
    _daemon_thread: threading.Thread | None = field(default=None, repr=False)
    _daemon_loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _ready_event: threading.Event | None = field(default=None, repr=False)
    _startup_error: BaseException | None = field(default=None, repr=False)

    def _build_uvicorn_server(self) -> uvicorn.Server:
        host, port = self._ensure_address()
        app = self.server.streamable_http_app()
        log_level = self.uvicorn_log_level or self.server.settings.log_level
        if isinstance(log_level, str):
            log_level = log_level.lower()
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            loop="asyncio",
            log_level=log_level,
            access_log=False,
        )
        return uvicorn.Server(config)

    @property
    def url(self) -> str:
        host, port = self._ensure_address()
        path = self._ensure_path()
        return f"http://{host}:{port}{path}"

    def runner(self) -> Callable[[], None]:
        """Return a sync function that blocks while serving the MCP HTTP endpoint."""
        bound_socket = self._bound_socket

        async def _serve() -> None:
            uvicorn_server = self._build_uvicorn_server()
            if bound_socket is not None:
                await uvicorn_server.serve(sockets=[bound_socket])
            else:
                await uvicorn_server.serve()

        def _run() -> None:
            asyncio.run(_serve())

        return _run

    @asynccontextmanager
    async def run_in_background(self) -> AsyncIterator[None]:
        """Async context manager that runs the HTTP server on a background task."""
        await self.start()
        try:
            yield
        finally:
            await self.stop()

    def get_state(self) -> Any:
        """Return the cached state instance (if provided)."""
        return self._state_instance

    # -- Async start / stop -----------------------------------------------

    async def start(self, max_retries: int = 5) -> None:
        """Start the HTTP server in the background with retry on port conflict."""
        if self._server_task is not None:
            raise RuntimeError("Server already running")

        last_error = None
        for attempt in range(max_retries):
            try:
                if self._bound_socket is None:
                    self._bound_socket = _create_bound_socket(self.host)
                    self.port = self._bound_socket.getsockname()[1]
                    self.server.settings.port = self.port

                uvicorn_server = self._build_uvicorn_server()
                self._uvicorn_server = uvicorn_server
                self._server_task = asyncio.create_task(uvicorn_server.serve(sockets=[self._bound_socket]))
                host, port = self._ensure_address()
                await _wait_for_server_ready(host, port)
                return
            except (OSError, RuntimeError) as e:
                last_error = e
                if self._uvicorn_server is not None:
                    self._uvicorn_server.should_exit = True
                if self._server_task is not None:
                    self._server_task.cancel()
                    try:
                        await self._server_task
                    except (asyncio.CancelledError, Exception):
                        pass
                self._server_task = None
                self._uvicorn_server = None
                if self._bound_socket is not None:
                    try:
                        self._bound_socket.close()
                    except OSError:
                        pass
                    self._bound_socket = None
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.1 * (attempt + 1))

        raise RuntimeError(f"Failed to start MCP server after {max_retries} attempts: {last_error}")

    async def stop(self) -> None:
        """Stop the background HTTP server."""
        if self._server_task is None:
            return
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        try:
            await self._server_task
        except asyncio.CancelledError:
            pass
        finally:
            self._server_task = None
            self._uvicorn_server = None

    # -- Sync bridge (for use from synchronous CAW Agent API) ---------------

    def start_sync(self, timeout: float = 30.0) -> None:
        """Start the server from a synchronous context.

        Spawns a daemon thread with its own event loop, starts the server
        inside it, and blocks the calling thread until the server is ready.
        """
        if self._daemon_thread is not None:
            raise RuntimeError("Server already running (sync)")

        ready = threading.Event()
        self._ready_event = ready
        self._startup_error = None

        def _daemon_main() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._daemon_loop = loop

            async def _run() -> None:
                try:
                    await self.start()
                except BaseException as exc:
                    self._startup_error = exc
                    return
                finally:
                    ready.set()

                # Keep the loop alive while the server task runs
                if self._server_task is not None:
                    try:
                        await self._server_task
                    except asyncio.CancelledError:
                        pass

            try:
                loop.run_until_complete(_run())
            finally:
                # Cancel lingering tasks (e.g. SSE shutdown watchers) to avoid
                # "Task was destroyed but it is pending!" warnings on exit.
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()

        thread = threading.Thread(target=_daemon_main, daemon=True)
        self._daemon_thread = thread
        thread.start()

        ready.wait(timeout=timeout)
        if self._startup_error is not None:
            # Join the thread since startup failed
            thread.join(timeout=5)
            self._daemon_thread = None
            raise RuntimeError(f"MCP server failed to start: {self._startup_error}") from self._startup_error

    def stop_sync(self, timeout: float = 10.0) -> None:
        """Stop the server from a synchronous context."""
        if self._daemon_thread is None or self._daemon_loop is None:
            return

        loop = self._daemon_loop
        future = asyncio.run_coroutine_threadsafe(self.stop(), loop)
        try:
            future.result(timeout=timeout)
        except Exception:
            pass

        self._daemon_thread.join(timeout=timeout)
        self._daemon_thread = None
        self._daemon_loop = None
        self._ready_event = None

    # -- Internal helpers -------------------------------------------------

    def _ensure_address(self) -> tuple[str, int]:
        if self.port is None:
            self.port = self.server.settings.port
        return self.host, self.port

    def _ensure_path(self) -> str:
        if self.path is None:
            self.path = f"/mcp/{self.server_id}/{uuid_mod.uuid4().hex[:6]}"
            self.server.settings.streamable_http_path = self.path
        return self.path


# -- Factory -------------------------------------------------------------------


def create_mcp_http_server_bundle(
    server_id: str,
    *,
    display_name: str,
    state_factory: Callable[[], Any] | None = None,
    state_instance: Any = None,
    state_display_name: str | None = None,
    state_shutdown: Callable[[Any], None | Awaitable[None]] | None = None,
    uvicorn_log_level: str | None = "error",
    state_logger: Callable[[str], None] | None = None,
    **fastmcp_kwargs: Any,
) -> MCPServerHandle:
    """Create a FastMCP server plus helpers for running it over HTTP."""
    _require_fastmcp()
    lifespan = fastmcp_kwargs.pop("lifespan", None)
    if lifespan is not None and (state_factory is not None or state_instance is not None):
        raise ValueError("Provide either lifespan or state_factory/state_instance, not both.")
    if state_factory is not None and state_instance is not None:
        raise ValueError("Provide either state_factory or state_instance, not both.")

    if state_factory is not None:
        state_instance = state_factory()

    state = state_instance
    if state is not None:
        active = 0

        @asynccontextmanager
        async def managed_lifespan(_server: FastMCP):
            nonlocal active
            first_start = active == 0
            if first_start and state_display_name:
                message = f"Initializing {state_display_name}"
                if state_logger:
                    state_logger(message)
            active += 1
            try:
                yield state
            finally:
                active -= 1
                if active == 0:
                    if state_display_name:
                        message = f"Shutting down {state_display_name}"
                        if state_logger:
                            state_logger(message)
                    if state_shutdown is not None:
                        result = state_shutdown(state)
                        if isinstance(result, Awaitable):
                            await result

        lifespan = managed_lifespan

    streamable_path = f"/mcp/{server_id}/{uuid_mod.uuid4().hex[:6]}"

    bound_socket = _create_bound_socket("127.0.0.1")
    port = bound_socket.getsockname()[1]

    server = FastMCP(
        name=display_name,
        host="127.0.0.1",
        port=port,
        streamable_http_path=streamable_path,
        lifespan=lifespan,
        **fastmcp_kwargs,
    )
    return MCPServerHandle(
        server_id=server_id,
        server=server,
        host="127.0.0.1",
        port=port,
        path=streamable_path,
        _state_instance=state_instance,
        uvicorn_log_level=uvicorn_log_level,
        _bound_socket=bound_socket,
    )


def create_stateless_tool_server(
    funcs: list[Callable[..., Any]],
    *,
    server_id: str | None = None,
    display_name: str = "stateless_tools",
) -> MCPServerHandle:
    """Bundle plain functions into a single `MCPServerHandle`.

    Each function is registered as an MCP tool.  Functions may optionally be
    decorated with `mcp_tool` or `tool` to supply
    metadata; bare functions are registered using their name and docstring.
    """
    sid = server_id or f"general_{uuid_mod.uuid4().hex[:6]}"
    handle = create_mcp_http_server_bundle(sid, display_name=display_name)
    for func in funcs:
        register_tool(handle.server, func)
    return handle


# -- Name sanitization ---------------------------------------------------------

_INVALID_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_\-.]")


def _sanitize_tool_name(name: str) -> str:
    """Replace characters invalid in MCP tool names with underscores."""
    return _INVALID_TOOL_NAME_RE.sub("_", name)


# -- Subagent tool factory ----------------------------------------------------


@dataclass
class SubagentState:
    """Lifespan state for a subagent tool server."""

    name: str
    description: str
    system_prompt: str
    model: str
    traj_dir: str
    jsonl_path: str
    tools: Any = None
    tool_servers: list = field(default_factory=list)
    mcp_servers: list = field(default_factory=list)
    subagents: list = field(default_factory=list)


def _run_subagent_blocking(
    prompt: str,
    system_prompt: str,
    model: str,
    traj_dir: str,
    jsonl_path: str,
    subagent_name: str,
    tools: Any = None,
    tool_servers: list | None = None,
    mcp_servers: list | None = None,
    subagents: list | None = None,
) -> str:
    """Run a single-turn subagent synchronously (called from a thread).

    Returns ``result_text`` with an optional trajectory marker appended.
    """
    from caw import Agent

    agent = Agent(
        system_prompt=system_prompt,
        model=model or None,
        tools=tools,
        data_dir=None,
    )

    for ts in tool_servers or []:
        agent.add_tool_server(ts)

    for srv in mcp_servers or []:
        agent.add_mcp_server(srv)

    for sub in subagents or []:
        agent.add_subagent(sub)

    try:
        with agent.start_session() as session:
            turn = session.send(prompt)
            traj = session.trajectory
    except Exception as e:
        return f"Error: {e}"

    result_text = turn.result
    traj_dict = traj.to_dict()

    # Write subagent events to parent's JSONL
    if traj.turns and jsonl_path:
        try:
            from caw.storage import JsonlWriter

            writer = JsonlWriter(jsonl_path, subagent=subagent_name)
            for i, t in enumerate(traj.turns):
                writer.write_turn_events(t, i)
        except Exception:
            pass

    # Write trajectory to file and embed marker in response
    traj_marker = ""
    if traj_dict and traj_dir:
        traj_id = str(uuid_mod.uuid4())
        traj_path = os.path.join(traj_dir, f"{traj_id}.json")
        try:
            os.makedirs(traj_dir, exist_ok=True)
            with open(traj_path, "w") as f:
                json.dump(traj_dict, f, indent=2)
            traj_marker = f"{_TRAJ_MARKER_PREFIX}{traj_id}{_TRAJ_MARKER_SUFFIX}"
        except OSError:
            pass

    return result_text + traj_marker


def create_subagent_tool_server(
    spec: Any,
    traj_dir: str,
    jsonl_path: str | None = None,
) -> MCPServerHandle:
    """Create an HTTP tool server that exposes a subagent as a callable tool.

    Parameters
    ----------
    spec
        An ``AgentSpec`` with ``name``, ``description``, ``system_prompt``, ``model``.
    traj_dir
        Directory where subagent trajectory JSON files are written.
    jsonl_path
        Path to the parent session's JSONL log (for interleaved subagent events).
    """
    tool_name = _sanitize_tool_name(spec.name)

    state = SubagentState(
        name=spec.name,
        description=spec.description,
        system_prompt=spec.system_prompt,
        model=spec.model or "",
        traj_dir=traj_dir,
        jsonl_path=jsonl_path or "",
        tools=getattr(spec, "tools", None),
        tool_servers=list(getattr(spec, "tool_servers", None) or []),
        mcp_servers=list(getattr(spec, "mcp_servers", None) or []),
        subagents=list(getattr(spec, "subagents", None) or []),
    )

    handle = create_mcp_http_server_bundle(
        "subagent",
        display_name=f"caw-subagent-{spec.name}",
        state_instance=state,
    )

    @mcp_tool(name=tool_name, description=spec.description)
    async def subagent_tool(prompt: str, ctx: Context) -> str:
        s: SubagentState = get_state_from_context(ctx)
        return await asyncio.to_thread(
            _run_subagent_blocking,
            prompt,
            s.system_prompt,
            s.model,
            s.traj_dir,
            s.jsonl_path,
            s.name,
            s.tools,
            s.tool_servers,
            s.mcp_servers,
            s.subagents,
        )

    register_tool(handle.server, subagent_tool)
    return handle
