# Subagents

A subagent is a child agent that the parent can invoke as a tool. Register an
[`AgentSpec`][caw.AgentSpec] and caw exposes it to the parent automatically; when the parent
calls it, the subagent runs its own session and its full trajectory is captured.

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
```

## Nested trajectories and usage roll-up

Each subagent invocation attaches a nested [`Trajectory`][caw.Trajectory] to the parent's
`ToolUse` block, so you can inspect what the child did:

```python
for sub in traj.subagent_trajectories:
    print(f"  subagent: {sub.agent}, {sub.num_turns} turns, ${sub.usage.cost_usd:.4f}")
```

Usage rolls up: `traj.usage` is the parent's own consumption, while `traj.total_usage` is the
parent **plus all nested subagents** (recursively). This is the number to use for total cost.

## Configuring a subagent

[`AgentSpec`][caw.AgentSpec] carries the same knobs as an `Agent`: `system_prompt`, `model`,
`reasoning`, `tools`, plus its own `tool_servers`, `mcp_servers`, and even nested `subagents`.
That means a subagent can have its own tools and its own children.

```python
AgentSpec(
    name="researcher",
    description="Searches the web and summarizes findings",
    system_prompt="You research topics thoroughly.",
    model="opus",
    tools=ToolGroup.READER | ToolGroup.WEB,
)
```

## Full example

[`examples/subagent.py`](https://github.com/zzjas/caw/blob/main/examples/subagent.py) shows a
senior-engineer agent delegating code review to a subagent and inspecting the nested
trajectory:

```python
--8<-- "examples/subagent.py"
```
