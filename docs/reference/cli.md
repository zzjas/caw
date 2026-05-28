# CLI reference

caw installs two console scripts: **`caw`** (the main multi-command app) and **`caw-traj`**
(a standalone trajectory inspector, also available as `caw traj`).

## `caw`

Top-level commands: `doctor` ([provider health](../guides/health.md)), `auth`
([Docker credentials](../guides/docker-credentials.md)), `viewer` and `traj`
([trajectory viewer](../guides/trajectory-viewer.md)).

::: mkdocs-typer2
    :module: caw.cli
    :name: caw

## `caw-traj`

The standalone trajectory inspector. Prints a compact, step-indexed view of a saved
trajectory and can expand individual steps.

::: mkdocs-click
    :module: caw.traj_cli
    :command: app
