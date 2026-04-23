"""CLI subcommands for `caw auth`."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer


app = typer.Typer(help="Manage credentials for Docker containers.")


@app.command()
def setup(
    agents: Annotated[
        Optional[list[str]],
        typer.Option("--agents", "-a", help="Agents to include (claude, codex, or all)"),
    ] = None,
    source_home: Annotated[
        str,
        typer.Option("--source-home", help="Source home directory to read credentials from"),
    ] = str(Path.home()),
):
    """Snapshot credentials and write the container setup bundle into ~/.caw/auth/.

    Host credential files are not modified; they are bind-mounted into the
    container at run time via `caw auth docker-flags`.
    """
    from .collector import setup as do_setup

    do_setup(agents=agents or ["all"], source_home=source_home)


@app.command()
def teardown(
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Show what would be done")] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Delete even if host symlinks point into the auth dir"),
    ] = False,
):
    """Remove ~/.caw/auth/. Host credential files are untouched.

    Refuses to run if host credentials are still symlinks into the auth
    directory (leftover from the old symlink-based design). Use `--force`
    to override — but you will have to re-authenticate every agent.
    """
    from . import TeardownWouldOrphanSymlinksError, teardown as do_teardown
    from .collector import AUTH_DIR

    if dry_run:
        if AUTH_DIR.exists():
            typer.echo(f"Would remove: {AUTH_DIR}")
        else:
            typer.echo(f"Nothing to remove: {AUTH_DIR} does not exist.")
        return

    if not AUTH_DIR.exists():
        typer.echo(f"Nothing to remove: {AUTH_DIR} does not exist.")
        return

    try:
        do_teardown(force=force)
    except TeardownWouldOrphanSymlinksError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
    typer.echo(f"Removed {AUTH_DIR}.")


@app.command("status")
def status_cmd(
    agents: Annotated[
        Optional[list[str]],
        typer.Option("--agents", "-a", help="Agents to show"),
    ] = None,
):
    """Show token expiry, last modified, and docker mount flags."""
    from .status import status as do_status

    do_status(agents=agents)


@app.command("docker-flags")
def docker_flags():
    """Output the -v flags for docker (one per bind mount, space-separated)."""
    from .status import get_docker_flags as do_get_docker_flags

    try:
        typer.echo(do_get_docker_flags())
    except FileNotFoundError:
        typer.echo("Error: manifest.json not found. Run `caw auth setup` first.", err=True)
        raise typer.Exit(1)
