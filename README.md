<h1 align="center">caw: Coding Agent Wrapper</h1>
<p align="center">
  <a href="https://pypi.org/project/coding-agent-wrapper/"><img src="https://img.shields.io/pypi/v/coding-agent-wrapper.svg" alt="PyPI version"></a>
  <a href="https://pypi.org/project/coding-agent-wrapper/"><img src="https://img.shields.io/pypi/pyversions/coding-agent-wrapper.svg" alt="Python versions"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="https://github.com/zzjas/caw/actions/workflows/lint.yml"><img src="https://img.shields.io/github/actions/workflow/status/zzjas/caw/lint.yml?label=lint" alt="Lint status"></a>
  <a href="https://zzjas.github.io/caw/"><img src="https://img.shields.io/badge/docs-mkdocs--material-blue.svg" alt="Documentation"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff"></a>
</p>

<p align="center">
  <a href="https://zzjas.github.io/caw/">Docs</a> ·
  <a href="https://zzjas.github.io/caw/getting-started/quickstart/">Quickstart</a> ·
  <a href="examples/">Examples</a>
</p>

---

**caw** (Coding Agent Wrapper) wraps multiple coding-agent CLIs — [Claude Code](https://docs.claude.com/en/docs/claude-code), [Codex](https://github.com/openai/codex), and [opencode](https://github.com/sst/opencode) — behind a single `Agent` / `Session` API. Swap providers without changing your code, attach MCP tool servers, capture structured trajectories, and manage credentials for Docker containers. caw aims at the common cases with a small, ergonomic API — if you need fine-grained control over agent behavior, reach for the underlying agent SDKs; caw isn't trying to replace them.

## Install

caw is on PyPI as `coding-agent-wrapper`. The recommended path is [uv](https://docs.astral.sh/uv/):

```bash
uv add coding-agent-wrapper            # use as a library in a uv-managed project
uv tool install coding-agent-wrapper   # install just the CLI (caw, caw-traj) globally
```

Plain `pip` works too:

```bash
pip install coding-agent-wrapper
```

Requires Python 3.10+. You also need at least one provider CLI installed and authenticated (`claude`, `codex`, or `opencode`) — run `caw doctor` to see what caw can find.

For local development:

```bash
uv sync --extra dev
```

## Quick start

```python
from caw import Agent

agent = Agent()  # defaults to claude_code
traj = agent.completion("Explain what this repository does")

print(traj.result)
print(f"{traj.usage.total_tokens} tokens, ${traj.usage.cost_usd:.4f}")
```

### Multi-turn session

```python
agent = Agent(model="claude-opus-4-7", reasoning="high")
agent.set_system_prompt("You are a security reviewer.")

with agent.start_session() as session:
    print(session.send("Review src/auth.py for vulnerabilities").result)
    print(session.send("Now check src/api.py").result)
# session.end() runs on exit and returns the full Trajectory
```

### Swap providers — same code

The most common way is the `CAW_PROVIDER` env var — set it once and every `Agent()` picks it up:

```bash
export CAW_PROVIDER=codex
```

```python
from caw import Agent
agent = Agent()                        # uses whatever CAW_PROVIDER says
```

Or hand caw a fallback order and let it pick whichever is installed & healthy at runtime:

```python
agent = Agent(provider=["claude", "codex", "opencode"])
traj = agent.completion("Reply with a one-line hello.")
print(f"[{traj.agent}] {traj.result}")  # whichever provider handled it
```

### Give the agent tools

Decorate a Python function with `@tool` and pass it in — caw stands up a tool server for you:

```python
from caw import Agent, tool

@tool(description="Add two numbers")
def add(a: int, b: int) -> int:
    return a + b

agent = Agent(stateless_tools=[add])
print(agent.completion("What is 17 plus 25? Use the tool.").result)
```

For stateful tools (shared state across calls in a session), subclass `ToolKit` — see the [guide](https://zzjas.github.io/caw/guides/toolkit/).

### Inspect a trajectory

Every call returns a structured `Trajectory`. Save it and browse it later:

```python
with Agent(data_dir="caw_data").start_session(traj_path="run.json") as session:
    session.send("List the Python files here and count them.")
```

Two viewers ship with caw:

```bash
caw-traj run.json          # compact, step-indexed terminal view
caw viewer                 # web UI, open run.json by path in the browser
```

## What you can do

- **[Providers](https://zzjas.github.io/caw/guides/providers/)** — one API across Claude Code, Codex, and opencode.
- **[Auto-provider fallback](https://zzjas.github.io/caw/guides/auto-provider/)** — pick the first installed/healthy provider, fall back transparently.
- **[Models & tiers](https://zzjas.github.io/caw/guides/models-and-tiers/)** — portable `ModelTier` selection instead of hard-coded model strings.
- **[Sessions & resume](https://zzjas.github.io/caw/guides/resuming/)** — multi-turn conversations that resume across processes via a `resume_handle`.
- **[MCP tools](https://zzjas.github.io/caw/guides/mcp-servers/)**, **[ToolKit](https://zzjas.github.io/caw/guides/toolkit/)**, and **[subagents](https://zzjas.github.io/caw/guides/subagents/)** — give the agent tools, declaratively or as child agents.
- **[Provider health](https://zzjas.github.io/caw/guides/health/)** — raw availability/credential signals, with an optional live probe (`caw doctor`).
- **[Trajectory viewer](https://zzjas.github.io/caw/guides/trajectory-viewer/)** — browse saved trajectories in the browser or terminal.
- **[Docker credentials](https://zzjas.github.io/caw/guides/docker-credentials/)** — bind-mount agent OAuth credentials into containers without touching host files (`caw auth`).

Every interaction yields a structured [`Trajectory`](https://zzjas.github.io/caw/getting-started/concepts/) with turns, content blocks, token usage, and cost — persisted to JSONL when you pass `data_dir=`.

## Documentation

Full docs, guides, and API reference: **<https://zzjas.github.io/caw/>**. Machine-readable [`llms.txt`](https://zzjas.github.io/caw/llms.txt) / [`llms-full.txt`](https://zzjas.github.io/caw/llms-full.txt) are published alongside, since caw's users are often agents.

Runnable examples live in [`examples/`](examples/).

## License

[Apache-2.0](LICENSE)
