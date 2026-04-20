# caw

**Coding Agent Wrapper** — a Python library and CLI for orchestrating coding agents (Claude Code, Codex, Gemini CLI, Cursor) with a unified interface, MCP tool servers, and credential management for Docker containers.

## Install

```bash
pip install coding-agent-wrapper
```

Import `caw`:

```python
import caw
```

For local development:

```bash
pip install -e .
```

Requires Python 3.10+.

## Library: Unified Agent Interface

caw wraps multiple coding agent CLIs behind a single `Agent` / `Session` API. Swap providers without changing your code.

### Quick start

```python
from caw import Agent

agent = Agent()  # defaults to claude_code
traj = agent.completion("Explain what this repository does")
print(traj.result)
print(f"{traj.usage.total_tokens} tokens, ${traj.usage.cost_usd:.4f}")
```

### Multi-turn sessions

```python
from caw import Agent

agent = Agent(provider="claude_code", model="opus", reasoning="high")
agent.set_system_prompt("You are a security reviewer.")

with agent.start_session() as session:
    turn1 = session.send("Review src/auth.py for vulnerabilities")
    print(turn1.result)

    turn2 = session.send("Now check src/api.py")
    print(turn2.result)

# session.end() called automatically, returns full Trajectory
```

### Providers

| Provider | CLI | Provider name |
|----------|-----|---------------|
| Claude Code | `claude` | `claude_code` |
| Codex | `codex` | `codex` |

Set via constructor, environment variable, or at runtime:

```python
agent = Agent(provider="codex")
# or
os.environ["CAW_PROVIDER"] = "codex"
# or
agent.set_provider("codex")
```

### MCP tool servers

Attach MCP servers so the agent can call external tools:

```python
from caw import Agent, MCPServer

agent = Agent()
agent.add_mcp_server(MCPServer(
    name="my_db",
    command="python",
    args=["-m", "my_mcp_server"],
))
```

### ToolKit: declarative tool servers

Define tools as Python classes. caw spins up an HTTP MCP server automatically:

```python
from caw import Agent, ToolKit, tool

class UserDB(ToolKit, server_name="user_db"):
    def __init__(self):
        self.users = ["Alice", "Bob"]

    @tool(description="List all users")
    async def list_users(self) -> str:
        return ", ".join(self.users)

    @tool(description="Add a user")
    async def add_user(self, name: str) -> str:
        self.users.append(name)
        return f"Added {name}"

db = UserDB()
agent = Agent(system_prompt="You have access to a user database.")
agent.add_tool_server(db.as_server())

traj = agent.completion("Add Eve to the user database, then list all users")
```

### Subagents

Register child agents that the parent can invoke as tools:

```python
from caw import Agent, AgentSpec

reviewer = AgentSpec(
    name="security_reviewer",
    description="Reviews code for security issues",
    system_prompt="You are a security expert. Review the given code.",
)

agent = Agent()
agent.add_subagent(reviewer)
traj = agent.completion("Review the auth module for vulnerabilities")

# Subagent trajectories are captured:
for sub in traj.subagent_trajectories:
    print(f"  subagent: {sub.agent}, {sub.num_turns} turns")
```

### Data models

Every interaction produces a `Trajectory` with structured data:

```
Trajectory
├── agent, model, session_id, created_at
├── turns: list[Turn]
│   ├── input: str
│   ├── output: list[TextBlock | ThinkingBlock | ToolUse]
│   │   └── ToolUse.subagent_trajectory: Trajectory | None
│   ├── usage: UsageStats
│   └── duration_ms: int
├── usage: UsageStats (own)
└── total_usage: UsageStats (own + all nested subagents)
```

Sessions are persisted to JSONL in `caw_data/` by default.

### Environment variables

| Variable | Purpose |
|----------|---------|
| `CAW_PROVIDER` | Default provider (`claude_code`, `codex`) |
| `CAW_MODEL` | Default model name |
| `CAW_EFFORT` | Default reasoning effort (`high`, `medium`, `low`) |

---

## CLI: `caw auth` — Credential Management for Docker Containers

Manages coding agent OAuth credentials so they stay in sync between your host and Docker containers. Supports Claude Code, Codex, Gemini CLI, and Cursor.

```bash
caw auth setup                        # collect + symlink credentials
caw auth status                       # check symlink state and token expiry
docker run $(caw auth docker-flags) -v ./project:/work my-image
caw auth teardown                     # restore original files
```

See [`caw/auth/README.md`](caw/auth/README.md) for details on how it works, container setup, and supported agents.

## License

[Apache-2.0](LICENSE)
