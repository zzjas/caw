# Persistence

Pass `data_dir=` to an `Agent` and every session is persisted to disk — an incremental
JSONL event log plus a full trajectory snapshot, updated after each turn.

```python
from caw import Agent

agent = Agent(data_dir="caw_data")
with agent.start_session() as session:
    session.send("Remember the number 42.")
    session.send("What number did I just tell you?")
```

Without a `data_dir`, sessions run in memory and nothing is written.

## On-disk layout

[`SessionStore`][caw.SessionStore] writes one directory per session:

```
<data_dir>/sessions/<session_id>/
    traj.jsonl          # incremental append-only event log
    trajectory.json     # full trajectory, overwritten after each turn
    turns/
        000_input.txt
        000_raw_output.jsonl
        ...
```

- `traj.jsonl` is a per-event stream (metadata, user, thinking, text, tool_call, tool_result,
  turn_end) written via [`JsonlWriter`][caw.JsonlWriter], with file locking for concurrent
  safety. Subagent events are tagged with their name.
- `trajectory.json` is the complete [`Trajectory`][caw.Trajectory] as JSON — the same object
  you get from `session.end()`. It's overwritten after every turn so a crash still leaves the
  latest snapshot.
- `turns/NNN_*` keep the raw input and the backend's raw output per turn.

## Loading trajectories back

Read a saved trajectory into a read-only session:

```python
from caw import Session

session = Session.load_trajectory("caw_data/sessions/<id>/trajectory.json")
print(session.trajectory.result)
```

Or save the current one anywhere:

```python
session.save_trajectory("/tmp/run.json")
```

You can also point `start_session(traj_path=...)` at a file to have caw write the trajectory
there after each step in addition to (or instead of) the `data_dir` layout.

## Fast stats over many trajectories

For dashboards, spend limiters, and list views you usually only want the header/footer fields
(cost, model, timestamps, token totals). [`FastStats`](display-and-logging.md#faststats) reads
just the head and tail of each file — far faster than a full parse:

```python
from caw import FastStats

total = FastStats.directory_total_cost("caw_data")
for s in FastStats.iter_directory("caw_data"):
    print(s.model, f"${s.cost_usd:.4f}", s.total_tokens)
```

## `data_dir` and resuming

`data_dir` is also what lets a resumed session restore its prior trajectory and append new
turns to the original directory — see [Resuming sessions](resuming.md) for the full
with/without-`data_dir` matrix.
