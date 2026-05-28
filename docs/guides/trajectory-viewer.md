# Trajectory viewer

caw ships a small web UI for browsing saved trajectories — turns, content blocks, tool calls,
and nested subagent trajectories — plus a terminal inspector for agents.

## Web viewer

Start the server from the CLI:

```bash
caw viewer                 # auto host/port
caw viewer --port 8080     # fixed port
```

…or programmatically with [`start_viewer_server()`][caw.start_viewer_server], which returns a
[`ViewerServer`][caw.ViewerServer] handle:

```python
from caw.viewer import start_viewer_server

server = start_viewer_server()          # auto host/port
print(server.url)                       # http://localhost:<port>
server.check_status()                   # True / False
server.stop()
```

The viewer loads a trajectory JSON file by absolute path, passed as a query parameter:

```
http://localhost:<port>?path=/abs/path/to/trajectory.json
```

Full example — [`examples/traj_viewer.py`](https://github.com/zzjas/caw/blob/main/examples/traj_viewer.py)
runs a short session, saves the trajectory, and opens it in the viewer:

```python
--8<-- "examples/traj_viewer.py"
```

## Terminal inspector

For a quick, scriptable look (or to let another agent read a trajectory), use the `caw-traj`
CLI or the `caw traj` subcommand. It prints a compact, step-indexed view and can expand
specific steps:

```bash
caw-traj run.json                 # compact, step-indexed overview
caw-traj run.json --recursive     # include nested subagent steps
caw-traj run.json --step 7        # full detail for step 7
caw-traj run.json --step 7-10     # a range
caw-traj run.json --step 12/3     # a nested step under step 12
```

See the [CLI reference](../reference/cli.md) for every option.
