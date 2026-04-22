"""caw CLI — main entry point."""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import typer

from caw.auth.cli import app as auth_app
from caw.traj_cli import TrajectoryRenderError, inspect_trajectory

app = typer.Typer(
    name="caw",
    help="Coding Agent Wrapper — tools for managing coding agents.",
    no_args_is_help=True,
)

app.add_typer(auth_app, name="auth")


@app.command()
def viewer(
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="Host to bind to."),
    port: int = typer.Option(0, "--port", "-p", help="Port to bind to (0 = auto)."),
):
    """Launch the trajectory viewer web UI."""
    from caw.viewer import start_viewer_server

    server = start_viewer_server(
        host=host,
        port=port or None,
    )
    typer.echo(f"Trajectory viewer running at {server.url}")
    typer.echo("Press Ctrl+C to stop.")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        signal.pause()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


@app.command("traj")
def traj(
    path: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Path to the saved trajectory JSON file.",
    ),
    step: list[str] | None = typer.Option(
        None,
        "--step",
        "-s",
        help="Show full details for visible-step selectors like 7, 7-10, or 12/3-12/7. Repeat to add more selectors.",
    ),
    recursive: bool = typer.Option(
        False,
        "--recursive",
        "-r",
        help="Include nested visible subagent steps in the compressed listing.",
    ),
    text_chars: int = typer.Option(
        60,
        "--text-chars",
        min=10,
        help="Maximum characters to show in user/assistant previews.",
    ),
    input_chars: int = typer.Option(
        60,
        "--input-chars",
        min=10,
        help="Reserved for future tool-detail rendering; accepted for compatibility.",
    ),
):
    """Inspect a saved trajectory from the terminal."""

    try:
        typer.echo(
            inspect_trajectory(
                path,
                step=step or [],
                recursive=recursive,
                text_chars=text_chars,
                input_chars=input_chars,
            )
        )
    except TrajectoryRenderError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


def main():
    app()


if __name__ == "__main__":
    main()
