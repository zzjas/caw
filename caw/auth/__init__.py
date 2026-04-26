"""caw.auth — Credential management for Docker containers."""

import shutil
from pathlib import Path

from .collector import default_auth_dir, setup
from .manifest import Manifest, AgentManifest, ManifestFile
from .providers import PROVIDERS, AgentAuthProvider, CollectedFile
from .status import AuthFileStatus, get_docker_flags, get_status, status


class TeardownWouldOrphanSymlinksError(RuntimeError):
    """Raised when teardown would break host symlinks left by the old caw design."""


def _find_old_design_symlinks(auth_dir: Path) -> list[tuple[Path, Path]]:
    """Return (host_path, target_inside_auth_dir) pairs for any host files
    that are symlinks pointing into ``auth_dir``.

    The pre-bind-mount version of caw replaced host credential files with
    symlinks into ~/.caw/auth/. Removing ``auth_dir`` in that state would
    leave dangling symlinks with no backup. This helper lets teardown detect
    the situation and refuse.
    """
    manifest_path = auth_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = Manifest.load(manifest_path)
    host_home = Path(manifest.host_home)
    resolved_auth = auth_dir.resolve()

    dangerous: list[tuple[Path, Path]] = []
    for agent in manifest.agents.values():
        for mf in agent.files:
            host = host_home / mf.host_original
            if not host.is_symlink():
                continue
            try:
                target = Path(str(host.resolve(strict=False)))
            except OSError:
                continue
            try:
                target.relative_to(resolved_auth)
            except ValueError:
                continue
            dangerous.append((host, target))
    return dangerous


def teardown(auth_dir: str | Path | None = None, force: bool = False) -> None:
    """Remove the auth directory. Host credential files are never touched.

    Refuses to run if any host credential file is still a symlink into
    ``auth_dir`` (legacy state from the old symlink-based design), since
    removing the directory would leave dangling symlinks with no backup.
    Pass ``force=True`` to override.

    Args:
        auth_dir: Custom auth directory. Defaults to ~/.caw/auth/.
        force: Delete even if host symlinks point into ``auth_dir``.

    Raises:
        TeardownWouldOrphanSymlinksError: If host symlinks point into the
            auth directory and ``force`` is False.
    """
    target = Path(auth_dir) if auth_dir else default_auth_dir()
    if not target.exists():
        return

    if not force:
        dangerous = _find_old_design_symlinks(target)
        if dangerous:
            lines = [f"  {host} -> {t}" for host, t in dangerous]
            raise TeardownWouldOrphanSymlinksError(
                "Refusing to remove "
                f"{target}: host credential files still symlink into it "
                "(leftover from the old symlink-based design). Removing the "
                "directory would leave dangling symlinks and you would need "
                "to re-authenticate every agent.\n\n"
                "Do one of:\n"
                "  1. Replace each symlink with its real file first:\n"
                "     for f in <paths>; do cp --remove-destination "
                '"$(readlink -f "$f")" "$f"; done\n'
                "  2. Call teardown(force=True) if you accept re-auth.\n\n"
                "Affected symlinks:\n" + "\n".join(lines)
            )

    shutil.rmtree(target)


def __getattr__(name: str):
    # Re-resolve AUTH_DIR at access time so users who set CAW_AUTH_DIR after
    # `from caw.auth import AUTH_DIR` still see the override.
    if name == "AUTH_DIR":
        return default_auth_dir()
    raise AttributeError(name)


__all__ = [
    "AUTH_DIR",
    "AgentAuthProvider",
    "default_auth_dir",
    "AgentManifest",
    "AuthFileStatus",
    "CollectedFile",
    "Manifest",
    "ManifestFile",
    "PROVIDERS",
    "TeardownWouldOrphanSymlinksError",
    "get_docker_flags",
    "get_status",
    "setup",
    "status",
    "teardown",
]
