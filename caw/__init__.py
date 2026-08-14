"""caw - Coding Agent Wrapper."""

__version__ = "0.1.10"

from caw.agent import (
    Agent,
    Session,
    get_provider_models,
    get_provider_order,
    installed_providers,
    register_provider,
    set_provider_order,
)
from caw.auth import get_docker_flags as auth_get_docker_flags
from caw.auth import get_status as auth_get_status
from caw.auth import setup as auth_setup
from caw.display import Display, DisplayMode, get_global_display, set_global_display
from caw.faststats import FastStats
from caw.health import AuthSignal, ProviderHealth, check_providers
from caw.logger import AgentLogger
from caw.mcp import (
    MCPServerHandle,
    create_mcp_http_server_bundle,
    create_stateless_tool_server,
    create_subagent_tool_server,
    get_state_from_context,
    mcp_tool,
    register_tool,
)
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
from caw.providers.claudep import ClaudePProvider
from caw.providers.codex import CodexProvider
from caw.providers.opencode import OpencodeProvider
from caw.storage import JsonlWriter, SessionStore
from caw.toolkit import ToolKit, tool
from caw.viewer import ViewerServer, start_viewer_server

# Auto-register built-in providers
register_provider("claude_code", ClaudeCodeProvider)
register_provider("claude", ClaudeCodeProvider)
register_provider("cc", ClaudeCodeProvider)
register_provider("claudep", ClaudePProvider)
register_provider("codex", CodexProvider)
register_provider("opencode", OpencodeProvider)

__all__ = [
    "Agent",
    "AgentLogger",
    "AgentSpec",
    "AuthSignal",
    "ProviderHealth",
    "check_providers",
    "ClaudeCodeProvider",
    "ClaudePProvider",
    "CodexProvider",
    "OpencodeProvider",
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
    "set_provider_order",
    "get_provider_order",
    "get_provider_models",
    "installed_providers",
    "register_tool",
    "start_viewer_server",
    "tool",
    "ToolKit",
    "ViewerServer",
    "auth_setup",
    "auth_get_status",
    "auth_get_docker_flags",
]
