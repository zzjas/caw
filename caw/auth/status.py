"""Display status of auth files — token expiry, last modified, docker flags."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .collector import default_auth_dir
from .manifest import Manifest, ManifestFile

console = Console()


@dataclass
class AuthFileStatus:
    """Status of a single managed auth file."""

    agent: str
    file: str  # host_original relative path
    type: str  # "credential" or "config"
    strategy: str  # "bind" or "copy"
    exists: bool  # whether the backing file (host for bind, staged for copy) exists
    token_expiry: str | None  # human-readable token info, or None


def _bind_source(manifest: Manifest, mf: ManifestFile) -> Path:
    """Absolute host path for a bind-mounted credential file."""
    return Path(manifest.host_home) / mf.host_original


def _staged_path(auth_dir: Path, mf: ManifestFile) -> Path:
    """Path of the file inside the auth directory staging area."""
    return auth_dir / mf.src


def _backing_path(manifest: Manifest, auth_dir: Path, mf: ManifestFile) -> Path:
    """Authoritative source for reads (host file for bind, staged for copy)."""
    if mf.strategy == "bind":
        return _bind_source(manifest, mf)
    return _staged_path(auth_dir, mf)


def _check_token_expiry(path: Path, agent_name: str) -> str | None:
    """Check token expiry for known agents. Returns human-readable status or None."""
    try:
        if agent_name == "claude" and path.exists():
            with open(path) as f:
                creds = json.load(f)
            expires_at = creds.get("claudeAiOauth", {}).get("expiresAt")
            if expires_at:
                dt = datetime.fromtimestamp(expires_at / 1000, tz=timezone.utc)
                now = datetime.now(timezone.utc)
                if dt < now:
                    delta = now - dt
                    return f"EXPIRED ({_format_delta(delta)} ago)"
                else:
                    delta = dt - now
                    return f"valid ({_format_delta(delta)} remaining)"
    except Exception:
        pass
    return None


def _format_delta(delta) -> str:
    """Format a timedelta to a human-readable string."""
    total_seconds = int(delta.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    elif total_seconds < 3600:
        return f"{total_seconds // 60}m"
    elif total_seconds < 86400:
        return f"{total_seconds // 3600}h {(total_seconds % 3600) // 60}m"
    else:
        return f"{total_seconds // 86400}d {(total_seconds % 86400) // 3600}h"


def _format_mtime(path: Path) -> str:
    """Format last modified time of a file."""
    try:
        mtime = path.stat().st_mtime
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        return f"{_format_delta(delta)} ago"
    except Exception:
        return "unknown"


def get_status(
    agents: list[str] | None = None,
    auth_dir: str | Path | None = None,
) -> list[AuthFileStatus]:
    """Return structured status of all managed auth files.

    Credential freshness is read from the host file directly (the source of
    truth), not from the staged snapshot under ``auth_dir``.

    Args:
        agents: Agent names to include, or None for all.
        auth_dir: Custom auth directory. Defaults to ~/.caw/auth/.

    Returns:
        List of AuthFileStatus for each managed file.

    Raises:
        FileNotFoundError: If the manifest.json doesn't exist in auth_dir.
    """
    resolved_dir = Path(auth_dir) if auth_dir else default_auth_dir()
    manifest_path = resolved_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No manifest.json found at {manifest_path}")

    manifest = Manifest.load(manifest_path)
    agent_names = set(agents) if agents and "all" not in agents else set(manifest.agents.keys())

    results: list[AuthFileStatus] = []
    for agent_name, agent_manifest in manifest.agents.items():
        if agent_name not in agent_names:
            continue

        # Find the credential file for token-expiry lookup (read from host).
        cred_mf = next(
            (mf for mf in agent_manifest.files if mf.type == "credential"),
            None,
        )
        token_info = (
            _check_token_expiry(_backing_path(manifest, resolved_dir, cred_mf), agent_name) if cred_mf else None
        )

        for mf in agent_manifest.files:
            backing = _backing_path(manifest, resolved_dir, mf)
            results.append(
                AuthFileStatus(
                    agent=agent_name,
                    file=mf.host_original,
                    type=mf.type,
                    strategy=mf.strategy,
                    exists=backing.exists(),
                    token_expiry=token_info if mf.type == "credential" else None,
                )
            )

    return results


def get_docker_flags(auth_dir: str | Path | None = None) -> str:
    """Return the Docker ``-v`` flags for mounting the auth directory and credentials.

    Emits one directory bind mount for the staging area plus one file bind
    mount per credential, pointing directly at the host's original file. The
    credentials are never copied out of their original location.

    Args:
        auth_dir: Custom auth directory. Defaults to ~/.caw/auth/.

    Returns:
        A space-separated string of Docker ``-v`` flags, e.g.::

            -v /.../.caw/auth:/tmp/caw_auth:rw \
            -v /.../.claude/.credentials.json:/tmp/caw_auth/claude/credentials.json:rw

    Raises:
        FileNotFoundError: If the manifest.json doesn't exist in auth_dir.
    """
    resolved_dir = Path(auth_dir) if auth_dir else default_auth_dir()
    manifest_path = resolved_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No manifest.json found at {manifest_path}")

    manifest = Manifest.load(manifest_path)

    flags = [f"-v {resolved_dir}:{manifest.mount_point}:rw"]
    for agent_manifest in manifest.agents.values():
        for mf in agent_manifest.files:
            if mf.strategy != "bind":
                continue
            host_path = _bind_source(manifest, mf)
            container_path = f"{manifest.mount_point}/{mf.src}"
            flags.append(f"-v {host_path}:{container_path}:rw")
    return " ".join(flags)


def status(agents: list[str] | None = None, auth_dir: str | Path | None = None) -> None:
    """Show status of all managed auth files.

    Args:
        agents: Agent names to show, or None for all.
        auth_dir: Custom auth directory. Defaults to ~/.caw/auth/.
    """
    resolved_dir = Path(auth_dir) if auth_dir else default_auth_dir()
    manifest_path = resolved_dir / "manifest.json"
    if not manifest_path.exists():
        console.print("[yellow]No auth directory found.[/yellow] Run `caw auth setup` first.")
        return

    manifest = Manifest.load(manifest_path)
    agent_names = set(agents) if agents and "all" not in agents else set(manifest.agents.keys())

    table = Table(title="caw auth status", show_lines=True)
    table.add_column("Agent", style="bold")
    table.add_column("File", style="dim")
    table.add_column("Type")
    table.add_column("Strategy")
    table.add_column("Source")
    table.add_column("Last Modified")
    table.add_column("Token")

    for agent_name, agent_manifest in manifest.agents.items():
        if agent_name not in agent_names:
            continue

        cred_mf = next((mf for mf in agent_manifest.files if mf.type == "credential"), None)
        token_info = (
            _check_token_expiry(_backing_path(manifest, resolved_dir, cred_mf), agent_name) if cred_mf else None
        )

        for i, mf in enumerate(agent_manifest.files):
            backing = _backing_path(manifest, resolved_dir, mf)
            source_label = f"[dim]host[/dim] {backing}" if mf.strategy == "bind" else f"[dim]staged[/dim] {backing}"
            if not backing.exists():
                source_label = f"[red]missing[/red] {backing}"

            mtime = _format_mtime(backing) if backing.exists() else "[red]missing[/red]"
            token_col = token_info if (i == 0 and token_info) else ""

            table.add_row(
                agent_name if i == 0 else "",
                mf.host_original,
                mf.type,
                mf.strategy,
                source_label,
                mtime,
                token_col,
            )

    console.print(table)

    # Docker flags hint — print each -v on its own line for readability
    console.print("\n[dim]Docker mount flags:[/dim]")
    flags = get_docker_flags(resolved_dir)
    tokens = flags.split()
    for i in range(0, len(tokens), 2):
        console.print(f"[dim]  {tokens[i]} {tokens[i + 1]}[/dim]")
