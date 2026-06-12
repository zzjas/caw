"""CLI subcommands for `caw pricing` — view and edit token pricing."""

from __future__ import annotations

import typer

from caw import config, pricing


app = typer.Typer(
    help="View and edit caw's per-model token pricing (~/.caw/config.json).",
    no_args_is_help=True,
)


def _fmt(value: float | None) -> str:
    return f"{value:g}" if value is not None else "—"


@app.command("list")
def list_pricing() -> None:
    """Show effective per-1M-token rates for every agent/model, with its source."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title="caw pricing (USD / 1M tokens)", show_lines=False)
    table.add_column("Agent", style="bold")
    table.add_column("Model")
    table.add_column("Input", justify="right")
    table.add_column("Cached", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Source", style="dim")

    source_style = {"user": "green", "baked": "dim"}
    for row in pricing.describe_pricing():
        src = row["source"]
        style = source_style.get(src, "white")
        table.add_row(
            row["agent"],
            row["model"],
            _fmt(row["input"]),
            _fmt(row["cached_input"]),
            _fmt(row["output"]),
            f"[{style}]{src}[/{style}]",
        )

    console.print(table)
    console.print(f"[dim]config: {config.config_path()}[/dim]")


@app.command()
def get(agent: str, model: str) -> None:
    """Print the effective rates for a single AGENT and MODEL."""
    rates = pricing.get_pricing(agent, model)
    if not rates:
        typer.echo(f"No pricing for {agent} {model} (cost computes as $0).")
        raise typer.Exit(1)
    typer.echo(
        f"input={_fmt(rates.get('input'))} "
        f"cached_input={_fmt(rates.get('cached_input'))} "
        f"output={_fmt(rates.get('output'))}"
    )


@app.command()
def set(  # noqa: A001 — `set` is the subcommand name, matching `caw config set`.
    agent: str,
    model: str,
    input_: float | None = typer.Option(None, "--input", help="Input price per 1M tokens."),
    cached: float | None = typer.Option(None, "--cached", help="Cached-input price per 1M tokens."),
    output: float | None = typer.Option(None, "--output", help="Output price per 1M tokens."),
) -> None:
    """Set pricing for AGENT/MODEL, saved to ~/.caw/config.json.

    Only the flags you pass are written; unspecified fields keep their shipped
    (or previously set) value.
    """
    rates: dict[str, float] = {}
    if input_ is not None:
        rates["input"] = input_
    if cached is not None:
        rates["cached_input"] = cached
    if output is not None:
        rates["output"] = output
    if not rates:
        typer.echo("Error: pass at least one of --input / --cached / --output.", err=True)
        raise typer.Exit(1)

    canonical = config.canonical_provider(agent)
    pricing.set_user_pricing(canonical, model, rates)
    eff = pricing.get_pricing(canonical, model)
    typer.echo(
        f"Set {canonical} {model}: input={_fmt(eff.get('input'))} "
        f"cached_input={_fmt(eff.get('cached_input'))} output={_fmt(eff.get('output'))}"
    )


@app.command()
def unset(agent: str, model: str) -> None:
    """Remove a user pricing override for AGENT/MODEL (revert to shipped)."""
    canonical = config.canonical_provider(agent)
    if pricing.unset_user_pricing(canonical, model):
        if pricing.get_pricing(canonical, model):
            typer.echo(f"Unset {canonical} {model}; reverts to shipped pricing.")
        else:
            typer.echo(f"Unset {canonical} {model}; no shipped pricing (cost computes as $0).")
    else:
        typer.echo(f"No user override set for {canonical} {model}.")


@app.command()
def path() -> None:
    """Print the path to the user config file."""
    typer.echo(str(config.config_path()))
