# Quickstart

This page gets you from install to a running agent in about a minute. If you haven't yet,
[install caw](installation.md) and make sure at least one provider CLI (e.g. `claude`) is
authenticated — `caw doctor` will tell you.

## 60-second example

```python
from caw import Agent

agent = Agent()  # defaults to claude_code
traj = agent.completion("Explain what this repository does")

print(traj.result)
print(f"{traj.usage.total_tokens} tokens, ${traj.usage.cost_usd:.4f}")
```

`Agent.completion()` runs a single message and returns the complete
[`Trajectory`](concepts.md) — `traj.result` is the final text, and `traj.usage` carries the
token counts and cost.

## Multi-turn session

When you need follow-up turns that share context, open a [session](../guides/sessions.md):

```python
from caw import Agent

agent = Agent(provider="claude_code", model="opus", reasoning="high")
agent.set_system_prompt("You are a security reviewer.")

with agent.start_session() as session:
    print(session.send("Review src/auth.py for vulnerabilities").result)
    print(session.send("Now check src/api.py").result)
# session.end() runs on context-manager exit and returns the full Trajectory
```

This is the runnable [`examples/basic.py`](https://github.com/zzjas/caw/blob/main/examples/basic.py):

```python
--8<-- "examples/basic.py"
```

## Swap providers without changing code

The same code runs against any backend. Pin one explicitly:

```python
agent = Agent(provider="codex")
```

…or give caw a fallback order and let it use whatever is installed and healthy at runtime:

```python
agent = Agent(provider=["claude", "codex", "opencode"])
traj = agent.completion("Reply with a one-line hello.")
print(f"[{traj.agent}] {traj.result}")  # whichever provider handled it
```

See [Auto-provider mode](../guides/auto-provider.md) for the full fallback semantics.

## Where to go next

- [Concepts](concepts.md) — what a `Trajectory` contains and how usage rolls up.
- [Providers](../guides/providers.md) — the three backends and how to switch.
- [Sessions](../guides/sessions.md) and [Resuming](../guides/resuming.md) — multi-turn and
  cross-process conversations.
- [ToolKit](../guides/toolkit.md), [MCP servers](../guides/mcp-servers.md), and
  [Subagents](../guides/subagents.md) — give the agent tools.
