---
title: caw — one interface for every coding agent
hide:
  - navigation
---

# caw

<p style="font-size: 1.2rem; color: var(--md-default-fg-color--light);">
One interface for every coding agent.
</p>

**caw** (Coding Agent Wrapper) is a Python library and CLI that wraps multiple coding-agent
CLIs — [Claude Code](https://docs.claude.com/en/docs/claude-code), [Codex](https://github.com/openai/codex),
and [opencode](https://github.com/sst/opencode) — behind a single `Agent` / `Session` API.
Swap providers without changing your code, attach MCP tool servers, capture structured
trajectories, and manage credentials for Docker containers.

caw aims at the common cases with a small, ergonomic API — if you need fine-grained control
over agent behavior, reach for the underlying agent SDKs; caw isn't trying to replace them.

```python
from caw import Agent

agent = Agent()  # defaults to claude_code
traj = agent.completion("Explain what this repository does")
print(traj.result)
print(f"{traj.usage.total_tokens} tokens, ${traj.usage.cost_usd:.4f}")
```

[Get started](getting-started/installation.md){ .md-button .md-button--primary }
[Quickstart](getting-started/quickstart.md){ .md-button }
[API reference](reference/api/agent.md){ .md-button }

## Why caw

- **One API, three backends.** Pin a provider, or give caw a fallback order and let it pick
  whatever is installed and healthy at runtime. See [Providers](guides/providers.md) and
  [Auto-provider mode](guides/auto-provider.md).
- **Portable model selection.** Use [`ModelTier`](guides/models-and-tiers.md) so "strongest"
  and "fast" resolve per provider — no hard-coded model strings.
- **Multi-turn sessions that resume across processes.** Grab a
  [`resume_handle`](guides/resuming.md) and continue the conversation later, even in a
  different process or without a `data_dir`.
- **Tools your way.** Attach [MCP servers](guides/mcp-servers.md), define tools declaratively
  with [`ToolKit`](guides/toolkit.md), or register [subagents](guides/subagents.md) the parent
  can call.
- **Structured trajectories.** Every interaction yields a [`Trajectory`](getting-started/concepts.md)
  with turns, content blocks, token usage, and cost — persisted to JSONL and viewable in the
  [trajectory viewer](guides/trajectory-viewer.md).
- **Health checks & credentials.** Probe provider [health](guides/health.md) and bind-mount
  OAuth credentials into [Docker containers](guides/docker-credentials.md) without touching
  host files.

## Install

```bash
pip install coding-agent-wrapper
```

Requires Python 3.10+. See [Installation](getting-started/installation.md) for the CLI
prerequisites and dev setup.

## For agents

These docs are also published as machine-readable
<a href="llms.txt"><code>llms.txt</code></a> (index) and
<a href="llms-full.txt"><code>llms-full.txt</code></a> (flattened) — handy when caw's own users
are agents.
