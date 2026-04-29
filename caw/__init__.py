"""caw - Coding Agent Wrapper."""

__version__ = "0.1.0"

from caw.agent import Agent, Session, register_provider
from caw.display import Display, DisplayMode, get_global_display, set_global_display
from caw.logger import AgentLogger
from caw.faststats import FastStats
from caw.storage import JsonlWriter, SessionStore
from caw.models import (
    AgentSpec,
    ContentBlock,
    InteractiveResult,
    MCPServer,
    MCPTool,
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
from caw.providers.claude_code import ClaudeCodeProvider
from caw.providers.codex import CodexProvider
from caw.mcp import (
    MCPServerHandle,
    create_mcp_http_server_bundle,
    create_stateless_tool_server,
    create_subagent_tool_server,
    get_state_from_context,
    mcp_tool,
    register_tool,
)
from caw.toolkit import ToolKit, tool
from caw.viewer import ViewerServer, start_viewer_server
from caw.auth import setup as auth_setup, get_status as auth_get_status, get_docker_flags as auth_get_docker_flags

# Auto-register built-in providers
register_provider("claude_code", ClaudeCodeProvider)
register_provider("claude", ClaudeCodeProvider)
register_provider("cc", ClaudeCodeProvider)
register_provider("codex", CodexProvider)

__all__ = [
    "Agent",
    "AgentLogger",
    "AgentSpec",
    "ClaudeCodeProvider",
    "CodexProvider",
    "JsonlWriter",
    "ContentBlock",
    "InteractiveResult",
    "Display",
    "DisplayMode",
    "FastStats",
    "get_global_display",
    "set_global_display",
    "MCPServer",
    "MCPServerHandle",
    "MCPTool",
    "ModelTier",
    "Provider",
    "ProviderSession",
    "Session",
    "SessionStore",
    "TextBlock",
    "ThinkingBlock",
    "ToolGroup",
    "ToolUse",
    "Trajectory",
    "Turn",
    "UsageStats",
    "create_mcp_http_server_bundle",
    "create_stateless_tool_server",
    "create_subagent_tool_server",
    "get_state_from_context",
    "mcp_tool",
    "register_provider",
    "register_tool",
    "start_viewer_server",
    "tool",
    "ToolKit",
    "ViewerServer",
    "auth_setup",
    "auth_get_status",
    "auth_get_docker_flags",
]
