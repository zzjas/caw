# Concepts

caw has a small, consistent vocabulary. Understanding these five types is enough to use most
of the library.

## Agent

[`Agent`][caw.Agent] is the configuration object and factory. You set the provider, model,
reasoning effort, system prompt, tool permissions, MCP servers, and subagents on it, then ask
it to do work. An `Agent` is cheap and reusable — it holds configuration, not a live
connection.

```python
from caw import Agent

agent = Agent(provider="claude_code", model="opus", reasoning="high")
agent.set_system_prompt("You are a security reviewer.")
```

Two ways to run work:

- **`agent.completion(message)`** — one-shot. Starts a session, sends one message, ends it,
  and returns the [`Trajectory`][caw.Trajectory].
- **`agent.start_session()`** — opens a multi-turn [`Session`](#session).

## Session

[`Session`][caw.Session] is a live, stateful conversation with a provider. Each
`session.send(message)` returns a [`Turn`](#turn); context carries across turns. Use it as a
context manager so it's finalized (and persisted) on exit:

```python
with agent.start_session() as session:
    session.send("Remember the number 42.")
    print(session.send("What number did I tell you?").result)
```

A session can be persisted and [resumed later](../guides/resuming.md) — even in another
process — via its `resume_handle`.

## Trajectory

[`Trajectory`][caw.Trajectory] is the complete, structured record of a session. It's what you
get back from `completion()` and `session.end()`, and what gets persisted to disk.

```
Trajectory
├── agent, model, session_id, created_at, completed_at
├── turns: list[Turn]
│   ├── input: str
│   ├── output: list[TextBlock | ThinkingBlock | ToolUse]
│   │   └── ToolUse.subagent_trajectory: Trajectory | None
│   ├── usage: UsageStats
│   └── duration_ms: int
├── usage: UsageStats        # this agent's own usage
└── total_usage: UsageStats  # own + all nested subagents (recursive)
```

Handy properties: `traj.result` (final text), `traj.num_turns`, `traj.total_tool_calls`,
`traj.subagent_trajectories`, `traj.is_complete`, and `traj.is_usage_limited`.

## Turn

[`Turn`][caw.Turn] is one request/response exchange: the user `input`, a list of `output`
content blocks, the `usage` for that turn, and `duration_ms`. Convenience accessors:

- `turn.result` — the last text block.
- `turn.tool_calls` — the [`ToolUse`][caw.ToolUse] blocks in this turn.

Content blocks are one of [`TextBlock`][caw.TextBlock], [`ThinkingBlock`][caw.ThinkingBlock],
or [`ToolUse`][caw.ToolUse] (which may carry a nested `subagent_trajectory`).

## UsageStats

[`UsageStats`][caw.UsageStats] holds `input_tokens`, `output_tokens`, cache token counts, and
`cost_usd`. They add together (`a + b`), and `total_tokens` sums input + output.

The distinction that matters: `trajectory.usage` is *this* agent's own consumption, while
`trajectory.total_usage` includes every nested [subagent](../guides/subagents.md) recursively.
For cost dashboards over many files, [`FastStats`](../guides/display-and-logging.md) extracts
these totals without parsing the whole trajectory.

## Putting it together

```python
traj = agent.completion("List the Python files here and count them.")

print(traj.result)                       # final answer text
print(traj.num_turns, "turn(s)")
for tc in traj.turns[-1].tool_calls:
    print("called", tc.name, "→", tc.output[:50])
print(f"${traj.total_usage.cost_usd:.4f} total")
```
