# Sessions

A [`Session`][caw.Session] is a live, multi-turn conversation. Open one with
`agent.start_session()`, send messages, and the context carries across turns.

```python
from caw import Agent

agent = Agent(provider="claude_code", model="opus", reasoning="high")
agent.set_system_prompt("You are a security reviewer.")

with agent.start_session() as session:
    turn1 = session.send("Review src/auth.py for vulnerabilities")
    print(turn1.result)

    turn2 = session.send("Now check src/api.py")
    print(turn2.result)
# session.end() runs on exit and returns the full Trajectory
```

Using the session as a context manager is the easy path — `__exit__` calls
[`session.end()`][caw.Session.end], which finalizes the trajectory, persists it, and stops any
tool servers. If you don't use `with`, call `session.end()` yourself.

## Where the agent runs — `cwd`

Every agent CLI takes its project root from the working directory: it is where
`claude` looks for `CLAUDE.md`, where relative paths resolve, and — for codex —
which tree `--sandbox workspace-write` may write to. Pass `cwd` to put a
session somewhere other than your own process's directory:

```python
agent = Agent(provider="codex", sandbox="workspace-write", cwd="/tmp/job-42")
agent.completion("Write a summary of this repo into notes.md")
```

It works the same on every provider, and on both the headless and interactive
paths. A `cwd` that is not a directory raises `NotADirectoryError` up front —
deliberately, because `subprocess` reports a missing working directory as
`FileNotFoundError`, which each provider would otherwise translate into "the
CLI is not installed".

## Options a provider does not support

Providers read the options they understand by name; anything else is reported:

```python
Agent(provider="claude_code", extra_config={"a": 1}).completion("hi")
# WARNING  claude_code: ignoring unsupported session option(s): extra_config —
#          this backend does not implement them, so they will have no effect.
```

An option a backend does not implement used to be dropped in silence, so a
typo — or a codex-only option handed to `claude` — left the call working and
quietly not doing what it said.

### `extra_config` (codex only)

codex takes arbitrary config overrides on the command line. Pass a flat dict of
dotted keys and caw turns each into a `-c key=value` flag with a TOML-encoded
value:

```python
agent = Agent(
    provider="codex",
    sandbox="workspace-write",
    cwd=work_dir,
    extra_config={
        "sandbox_workspace_write.exclude_slash_tmp": True,
        "sandbox_workspace_write.exclude_tmpdir_env_var": True,
    },
)
```

## One-shot vs. session

For a single message, `agent.completion(message)` is a convenience wrapper that opens a
session, sends once, and ends it:

```python
traj = agent.completion("Explain this code")
print(traj.result)
```

## Inspecting progress mid-session

`session.trajectory` is available during the session, not just after:

```python
with agent.start_session() as session:
    session.send("Remember the number 42.")
    session.send("What number did I just tell you?")

    traj = session.trajectory
    print(f"Turns: {traj.num_turns}")
    print(f"Total tool calls: {traj.total_tool_calls}")
    print(f"Total tokens: {traj.usage.total_tokens}")
```

## Async sends

`send_async()` runs the blocking send in a thread and processes overlapping calls in FIFO
order, so you can do async work while a turn is in flight:

```python
import asyncio

task = asyncio.create_task(session.send_async(prompt))
while not task.done():
    # ... do other async work ...
    await asyncio.sleep(0.5)
turn = await task
```

## Interactive mode

`agent.interactive(prompt)` hands control to the user's terminal — stdin/stdout/stderr are
inherited so the user talks to the agent directly, while caw captures a copy of the output.
All three providers support it (Claude Code, Codex, and opencode), each launching its own
full-screen TUI with your initial prompt.

Pass `select_provider=True` to choose which backend to launch at runtime: caw shows an
arrow-key menu of the *installed* providers (↑/↓ to move, `Enter` to choose, `q`/`Esc` to
cancel) and launches the one you pick, ignoring the agent's configured provider. Cancelling
the menu returns an `InteractiveResult` with exit code `130` without launching anything.
`caw.installed_providers()` exposes the same list (name + provider) for your own menus.

See [`examples/interactive.py`](https://github.com/zzjas/caw/blob/main/examples/interactive.py):

```python
--8<-- "examples/interactive.py"
```

## Auto-wait on usage limits

By default, when a provider reports a usage limit mid-session, `send()` sleeps until the limit
resets and then resumes automatically — transparently to you. Disable it per agent with
`Agent(..., auto_wait=False)` or globally with `CAW_AUTOWAIT=0`.

## Persistence and resuming

Pass `data_dir=` to persist a session to disk, and grab a `resume_handle` to continue it later
(even in another process). Those are covered in [Resuming sessions](resuming.md) and
[Persistence](persistence.md).
