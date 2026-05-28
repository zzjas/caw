# Display & logging

caw can stream a live, human-readable view of what the agent is doing, and/or emit a one-line
summary of every event to a logger of your choice.

## Console display

The [`Display`][caw.Display] class renders agent events (user messages, thinking, text, tool
calls and results, end-of-turn stats) to the terminal with Rich. It has four
[`DisplayMode`][caw.DisplayMode]s:

| Mode | Shows |
|------|-------|
| `FULL` | Everything, untruncated |
| `SHORT` | Truncated previews (the default) |
| `RESULT` | Only the final result, in a panel |
| `OFF` | Nothing |

The simplest way to control it is the `CAW_LOG` environment variable, which the global display
reads on first use:

```python
import os
os.environ["CAW_LOG"] = "full"   # or "short", "result", "off"

from caw import Agent
Agent().completion("Hello")      # streams output per CAW_LOG
```

Programmatically:

```python
from caw import Display, DisplayMode, set_global_display

set_global_display(Display(mode=DisplayMode.RESULT))
```

[`get_global_display()`][caw.get_global_display] returns the current instance (creating one
from `CAW_LOG` on first call); [`set_global_display(None)`][caw.set_global_display] silences
it.

## Structured logging

Separately from the pretty console output, you can route a **one-line summary per event** to
any logger via [`AgentLogger`][caw.AgentLogger]. Any object with `info` / `warn` / `error`
string methods satisfies the protocol — including the stdlib `logging.Logger` (after a tiny
`warn`→`warning` adapter) and your own sinks (e.g. a Redis logger).

```python
import logging
from caw import Agent

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent")
log.warn = log.warning  # satisfy the AgentLogger protocol

agent = Agent(logger=log)         # or agent.start_session(logger=log)
agent.completion("List the files here")
```

Every major event — user message, tool call, tool result, assistant text, thinking, turn-end
stats — is emitted as a compact line, in addition to the console `Display`.

## FastStats

[`FastStats`][caw.FastStats] extracts the frequently-needed header/footer fields (cost, model,
timestamps, token totals) from a trajectory file by reading only its head and tail — roughly
3× faster than a full parse on small files and 25×+ on multi-MB ones, with a full-parse
fallback when the fast path doesn't apply.

```python
from caw import FastStats

stats = FastStats.from_path("caw_data/sessions/<id>/trajectory.json")
print(stats.model, stats.cost_usd, stats.total_tokens)

# Aggregate across a directory:
total = FastStats.directory_total_cost("caw_data")
```

See the [Display & logging API reference](../reference/api/display.md) for the full surface.
