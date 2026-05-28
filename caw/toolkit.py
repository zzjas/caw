"""ToolKit base class and @tool decorator for declarative MCP tool servers.

Usage:

    from caw import Agent, ToolKit, tool

    class UserDB(ToolKit, server_name="user_db", display_name="User Database"):
        def __init__(self):
            self.users = ["Alice", "Bob"]

        @tool(description="List all users")
        async def list_users(self) -> str:
            return ", ".join(self.users)

    db = UserDB()
    agent = Agent(system_prompt="You have a user DB.", tool_servers=[db])
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import uuid as uuid_mod
from typing import Any, ClassVar

from caw.mcp import (
    Context,
    MCPServerHandle,
    create_mcp_http_server_bundle,
    get_state_from_context,
    register_tool,
)


# -- @tool decorator ----------------------------------------------------------


def tool(
    name: str | None = None,
    *,
    description: str | None = None,
    title: str | None = None,
    annotations: Any | None = None,
    structured_output: bool | None = None,
):
    """Mark a method as an MCP tool.  Does NOT modify the function itself."""

    def decorator(method):
        method._toolkit_tool_info = {
            "name": name,
            "description": description,
            "title": title,
            "annotations": annotations,
            "structured_output": structured_output,
        }
        return method

    return decorator


# -- ToolKit base class -------------------------------------------------------


class ToolKit:
    """Base class for declarative MCP tool servers.

    Subclass, decorate methods with ``@tool``, call ``as_server()``
    to get an `MCPServerHandle`.
    """

    _server_name: ClassVar[str] = ""
    _display_name: ClassVar[str] = ""
    _thread_safe: ClassVar[bool] = False

    def __init_subclass__(
        cls,
        server_name: str = "",
        display_name: str = "",
        thread_safe: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        cls._server_name = server_name or cls._server_name
        cls._display_name = display_name or cls._display_name
        if thread_safe:
            cls._thread_safe = True

    def as_server(self, server_id: str | None = None) -> MCPServerHandle:
        """Build and return an `MCPServerHandle` with all ``@tool`` methods registered."""
        cls = type(self)
        if cls._thread_safe and not hasattr(self, "_toolkit_lock"):
            self._toolkit_lock = threading.Lock()
        sid = server_id or f"{cls._server_name or cls.__name__}_{uuid_mod.uuid4().hex[:6]}"

        handle = create_mcp_http_server_bundle(
            sid,
            display_name=cls._display_name or cls._server_name or cls.__name__,
            state_instance=self,
        )

        for attr_name in dir(cls):
            attr = getattr(cls, attr_name, None)
            if attr is None:
                continue
            info = getattr(attr, "_toolkit_tool_info", None)
            if info is None:
                continue
            wrapper = _make_tool_func(attr, info)
            register_tool(handle.server, wrapper)

        return handle


# -- Wrapper generator --------------------------------------------------------


def _make_tool_func(method, info: dict[str, Any]):
    """Create an MCP-compatible free function from a ToolKit method.

    The generated wrapper:
    - Drops ``self`` from the signature.
    - Appends ``ctx: Context`` (or keeps it if the user already declared it).
    - At call time, retrieves the ToolKit instance from context and delegates.
    """
    sig = inspect.signature(method)
    params = list(sig.parameters.values())

    # Remove 'self'
    if params and params[0].name == "self":
        params = params[1:]

    # Check if the user declared a 'ctx' parameter
    user_has_ctx = any(p.name == "ctx" for p in params)

    # Build new parameter list: all user params (minus ctx if present) + ctx at end
    new_params = [p for p in params if p.name != "ctx"]
    ctx_param = inspect.Parameter(
        "ctx",
        inspect.Parameter.KEYWORD_ONLY,
        annotation=Context,
    )
    new_params.append(ctx_param)

    new_sig = sig.replace(parameters=new_params)

    is_async = asyncio.iscoroutinefunction(method)

    if is_async:

        async def wrapper(**kwargs):
            ctx = kwargs.pop("ctx")
            self_instance = get_state_from_context(ctx)
            if user_has_ctx:
                kwargs["ctx"] = ctx
            lock = getattr(self_instance, "_toolkit_lock", None)
            if lock is not None:
                with lock:
                    return await method(self_instance, **kwargs)
            return await method(self_instance, **kwargs)

    else:

        async def wrapper(**kwargs):
            ctx = kwargs.pop("ctx")
            self_instance = get_state_from_context(ctx)
            if user_has_ctx:
                kwargs["ctx"] = ctx
            lock = getattr(self_instance, "_toolkit_lock", None)
            if lock is not None:
                with lock:
                    return method(self_instance, **kwargs)
            return method(self_instance, **kwargs)

    tool_name = info.get("name") or method.__name__
    wrapper.__name__ = tool_name
    wrapper.__qualname__ = tool_name
    wrapper.__doc__ = info.get("description") or method.__doc__ or ""
    wrapper.__signature__ = new_sig
    # Set __annotations__ so typing.get_type_hints() can find the Context
    # parameter — FastMCP uses get_type_hints (not __signature__) to detect
    # which params to strip from the input schema and inject at call time.
    wrapper.__annotations__ = {
        p.name: p.annotation for p in new_sig.parameters.values() if p.annotation is not inspect.Parameter.empty
    }
    if new_sig.return_annotation is not inspect.Signature.empty:
        wrapper.__annotations__["return"] = new_sig.return_annotation

    # Attach _mcp_tool_info for register_tool()
    wrapper._mcp_tool_info = {
        "name": tool_name,
        "title": info.get("title"),
        "description": info.get("description") or method.__doc__ or "",
        "annotations": info.get("annotations"),
        "structured_output": info.get("structured_output"),
    }

    return wrapper
