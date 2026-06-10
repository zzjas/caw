"""caw CLI — main entry point."""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import typer

from caw.auth.cli import app as auth_app
from caw.config_cli import app as config_app
from caw.traj_cli import TrajectoryRenderError, inspect_trajectory

app = typer.Typer(
    name="caw",
    help="Coding Agent Wrapper — tools for managing coding agents.",
    no_args_is_help=True,
)

app.add_typer(auth_app, name="auth")
app.add_typer(config_app, name="config")


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


@app.command()
def doctor(
    live: bool = typer.Option(
        False,
        "--live",
        help="Round-trip a probe per provider to check it responds / isn't rate-limited (costs a request each).",
    ),
):
    """Show health/availability signals for each provider's CLI."""
    from rich.console import Console
    from rich.table import Table

    from caw.health import check_providers

    healths = check_providers(live=live)
    console = Console()

    table = Table(title="caw doctor", show_lines=True)
    table.add_column("Provider", style="bold")
    table.add_column("Installed")
    table.add_column("Binary", style="dim")
    table.add_column("Auth")
    if live:
        table.add_column("Probe")

    for h in healths:
        installed = "[green]✓[/green]" if h.installed else "[red]✗[/red]"
        if h.auth is None:
            auth = "[dim]unknown[/dim]"
        elif h.auth.token_expired:
            auth = f"[red]{h.auth.detail}[/red]"
        elif h.auth.present:
            auth = f"[green]{h.auth.detail}[/green]"
        else:
            auth = f"[yellow]{h.auth.detail}[/yellow]"

        row = [h.provider, installed, h.binary_path or "—", auth]
        if live:
            if not h.probed:
                probe = "[dim]skipped[/dim]"
            elif h.error:
                probe = f"[red]error: {h.error}[/red]"
            elif h.rate_limited:
                probe = f"[yellow]rate-limited (~{h.wait_minutes}m)[/yellow]"
            else:
                probe = "[green]responds[/green]"
            row.append(probe)
        table.add_row(*row)

    console.print(table)


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
