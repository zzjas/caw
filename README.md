# caw

**Coding Agent Wrapper** — a Python library and CLI for orchestrating coding agents (Claude Code, Codex, opencode) with a unified interface, MCP tool servers, and credential management for Docker containers.

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

### Resuming sessions across processes

Grab a `resume_handle` (a string) and store it anywhere — a database, a file, a
queue. Later, in a different process, resume the conversation:

```python
# Process 1: start, communicate, persist the handle.
agent = Agent(provider="claude_code")
session = agent.start_session()
session.send("My deploy target is staging-eu. Remember that.")
handle = session.resume_handle          # store this string
session.end()

# Process 2 (later, after a restart): resume by handle.
agent = Agent(provider="claude_code")
session = agent.resume_session(handle)
print(session.send("Where am I deploying?").result)   # -> "staging-eu"
session.end()
```

The handle is a **self-contained JSON string** carrying the backend's own resume
key, so resuming works even with no `data_dir` — the underlying CLI still has the
conversation:

```json
{"version": 1, "provider": "claude_code", "session_id": "bd260210-…", "resume_key": "bd260210-…"}
```

(`resume_key` is claude's session id, Codex's `thread_id`, or opencode's session
id — for codex/opencode it differs from `session_id`.) Send at least one message
before reading `resume_handle`; the backend assigns its key on the first
exchange. Works across all three providers.

> The handle grants resume access to the conversation — treat it like a secret,
> not an opaque random id.

`data_dir` is optional and additive:

| | without `data_dir` | with the original `data_dir` |
|---|---|---|
| backend conversation | resumed | resumed |
| caw trajectory | starts empty | full history restored |
| new turns | not persisted | appended to the original session dir |

### Providers

| Provider | CLI | Provider name |
|----------|-----|---------------|
| Claude Code | `claude` | `claude_code` |
| Codex | `codex` | `codex` |
| opencode | `opencode` | `opencode` |

Set via constructor, environment variable, or at runtime:

```python
agent = Agent(provider="codex")
# or
os.environ["CAW_PROVIDER"] = "codex"
# or
agent.set_provider("codex")
```

### Auto-provider mode

Don't want to hard-code one provider? Give caw a **fallback order** and let it
use whatever is available at runtime. caw selects the first *installed* provider
and, on the first send, transparently moves to the next one if that provider
fails (CLI missing, auth expired) or is rate-limited — no exception handling or
provider-picking on your side.

```python
import caw
from caw import Agent

caw.set_provider_order(["claude", "codex", "opencode"])  # set once, globally

agent = Agent(provider="auto")            # uses the global order
traj = agent.completion("Explain this repo")
print(f"[{traj.agent}] {traj.result}")    # whichever provider handled it
```

The order can come from (highest priority first):

```python
Agent(provider=["claude", "codex"])   # explicit per-agent order
caw.set_provider_order([...])          # global default, used by provider="auto"
os.environ["CAW_PROVIDER"] = "claude,codex,opencode"   # env var, comma list
```

A single name (`provider="claude"`) stays pinned — no fallback. Once a provider
produces the first turn the session is committed to it (conversation context
can't move across CLIs), so later failures propagate normally.

> In auto mode prefer a `ModelTier` (or no model) over a concrete model string:
> tiers are re-resolved per provider so model selection stays portable across
> the fallback. A concrete model string is dropped when falling back to a
> different provider.

See [`examples/auto_provider.py`](examples/auto_provider.py).

### Provider health / availability

Check whether a provider is set up correctly — without committing to using it.
caw reports **raw signals** (it forms no "available" verdict, so you write your
own predicate). The default check is fast and free; `live=True` round-trips a
tiny probe to confirm the provider responds and isn't rate-limited.

```python
from caw import Agent, check_providers

for h in check_providers():               # fast: no network, no token cost
    print(h.provider, h.installed, h.binary_path,
          h.auth.detail if h.auth else None)

# Compose your own notion of "available":
usable = [h.provider for h in check_providers()
          if h.installed and not (h.auth and h.auth.token_expired)]

# Per-agent, with an optional live probe (costs one request):
h = Agent(provider="codex").check_health(live=True)
if h.rate_limited:
    print(f"codex rate-limited, ~{h.wait_minutes}m until reset")
```

`ProviderHealth` exposes `installed`, `binary_path`, `auth` (an `AuthSignal`
with `present` / `token_expired` / `token_expires_at` / `detail`), and — after a
live probe — `probed` / `rate_limited` / `wait_minutes` / `error`. See
[`examples/health.py`](examples/health.py) or run `caw doctor`.

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
| `CAW_PROVIDER` | Default provider, or a comma-separated fallback order (`claude_code,codex,opencode`) |
| `CAW_MODEL` | Default model name |
| `CAW_EFFORT` | Default reasoning effort (`high`, `medium`, `low`) |

---

## CLI: `caw doctor` — Provider Health

Print a table of each provider's health signals — whether the CLI is installed,
where the binary is, and what caw can tell about its credentials.

```bash
caw doctor            # fast: installed + credential signals (no token cost)
caw doctor --live     # also probe each provider (costs one request each)
```

## CLI: `caw auth` — Credential Management for Docker Containers

Manages coding agent OAuth credentials so they stay in sync between your host and Docker containers. Supports Claude Code, Codex, and opencode. Host credential files are never modified — they are bind-mounted into the container at run time.

```bash
caw auth setup                        # snapshot configs, write mount manifest
caw auth status                       # token expiry, last modified, mount flags
docker run $(caw auth docker-flags) -v ./project:/work my-image
caw auth teardown                     # rm -rf ~/.caw/auth/  (host files untouched)
```

See [`caw/auth/README.md`](caw/auth/README.md) for details on how it works, container setup, and supported agents.

## License

[Apache-2.0](LICENSE)
