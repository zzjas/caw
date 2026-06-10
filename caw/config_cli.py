"""CLI subcommands for `caw config` — view and edit model configuration."""

from __future__ import annotations

import typer

from caw import config


app = typer.Typer(
    help="View and edit caw's per-provider model configuration (~/.caw/config.json).",
    no_args_is_help=True,
)


def _validate_tier(tier: str) -> str:
    tier = tier.lower()
    if tier not in config.TIERS:
        valid = ", ".join(config.TIERS)
        typer.echo(f"Error: unknown tier {tier!r}. Valid tiers: {valid}", err=True)
        raise typer.Exit(1)
    return tier


@app.command("list")
def list_models() -> None:
    """Show the effective model for every provider/tier, with its source."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title="caw models", show_lines=False)
    table.add_column("Provider", style="bold")
    table.add_column("Tier")
    table.add_column("Model")
    table.add_column("Source", style="dim")

    source_style = {"user": "green", "remote": "cyan", "baked": "dim", "unset": "yellow"}
    for row in config.describe_models():
        model = row["model"]
        model_cell = model if model else "[dim](provider default)[/dim]"
        src = row["source"]
        style = "magenta" if src.startswith("env:") else source_style.get(src, "white")
        table.add_row(row["provider"], row["tier"], model_cell, f"[{style}]{src}[/{style}]")

    console.print(table)
    console.print(f"[dim]config: {config.config_path()}[/dim]")


@app.command()
def get(provider: str, tier: str) -> None:
    """Print the effective model for a single PROVIDER and TIER."""
    tier = _validate_tier(tier)
    model = config.get_model(provider, tier)
    typer.echo(model if model else "(provider default)")


@app.command()
def set(provider: str, tier: str, model: str) -> None:
    """Set the model for PROVIDER/TIER, saved to ~/.caw/config.json."""
    tier = _validate_tier(tier)
    canonical = config.canonical_provider(provider)
    config.set_user_model(canonical, tier, model)
    typer.echo(f"Set {canonical} {tier} = {model}")


@app.command()
def unset(provider: str, tier: str) -> None:
    """Remove a user override for PROVIDER/TIER (revert to the default)."""
    tier = _validate_tier(tier)
    canonical = config.canonical_provider(provider)
    if config.unset_user_model(canonical, tier):
        fallback = config.get_model(canonical, tier)
        shown = fallback if fallback else "(provider default)"
        typer.echo(f"Unset {canonical} {tier}; now resolves to {shown}")
    else:
        typer.echo(f"No user override set for {canonical} {tier}.")


@app.command()
def path() -> None:
    """Print the path to the user config file."""
    typer.echo(str(config.config_path()))


@app.command()
def refresh() -> None:
    """Force a re-fetch of the remote default models into the local cache."""
    url = config.defaults_url()
    if not url:
        typer.echo("Remote defaults are disabled (CAW_DEFAULTS_URL is off).")
        raise typer.Exit(1)
    data = config.refresh_defaults()
    if data is None:
        typer.echo(f"Failed to fetch remote defaults from {url}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Refreshed defaults from {url}\nCached at {config.cache_path()}")
