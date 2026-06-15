"""User-editable model configuration for caw, stored under ``~/.caw``.

The default model for each ``(provider, tier)`` pair is no longer hardcoded in
the provider modules.  Instead it is resolved here, with this precedence
(highest first):

1. **Provider env var** — e.g. ``ANTHROPIC_MODEL`` / ``OPENCODE_MODEL`` — a
   per-process runtime override (unchanged from the old behaviour).
2. **User config** ``~/.caw/config.json`` — what ``caw config set`` edits.
3. **Remote defaults** — fetched from ``CAW_DEFAULTS_URL`` and cached under
   ``~/.caw/cache``.  This is the layer that lets the shipped defaults be
   updated *without* cutting a PyPI release: edit the JSON file on ``main`` and
   every install picks it up within the cache TTL.
4. **Baked-in defaults** — ``caw/defaults/models.json`` shipped in the wheel,
   used as the offline floor when nothing else is available.

The same ``caw/defaults/models.json`` file is both bundled in the wheel *and*
served at the default remote URL, so there is a single source of truth.

Relevant environment variables:

* ``CAW_HOME`` — base dir for caw state (default ``~/.caw``).
* ``CAW_DEFAULTS_URL`` — remote defaults URL; set to ``off``/empty to disable
  network fetches entirely.
* ``CAW_DEFAULTS_TTL`` — cache freshness window in seconds (default 86400).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

from caw.models import ModelTier

logger = logging.getLogger(__name__)

# Tiers we know how to configure.  Mirrors ``ModelTier``.
TIERS: tuple[str, ...] = tuple(t.value for t in ModelTier)

DEFAULT_DEFAULTS_URL = "https://raw.githubusercontent.com/zzjas/caw/main/caw/defaults/models.json"
_DEFAULT_TTL_SECONDS = 86_400  # 24h
_FETCH_TIMEOUT_SECONDS = 3.0

# Provider env-var overrides, keyed by (canonical provider, tier).  Kept here so
# the historical ``ANTHROPIC_MODEL`` / ``OPENCODE_MODEL`` overrides keep working.
_ENV_OVERRIDES: dict[tuple[str, str], str] = {
    ("claude_code", "strongest"): "ANTHROPIC_MODEL",
    ("claude_code", "fast"): "ANTHROPIC_SMALL_FAST_MODEL",
    ("opencode", "strongest"): "OPENCODE_MODEL",
    ("opencode", "fast"): "OPENCODE_SMALL_FAST_MODEL",
}

# Provider name aliases → canonical config key.  Matches the registry aliases in
# ``caw/__init__.py`` so ``caw config set claude …`` lands on ``claude_code``.
_PROVIDER_ALIASES: dict[str, str] = {
    "claude": "claude_code",
    "cc": "claude_code",
    # The subscription-backed TUI variant shares claude_code's model defaults,
    # env overrides, and pricing table.
    "claudep": "claude_code",
}


# --- paths ------------------------------------------------------------------


def caw_home() -> Path:
    """Base directory for caw state, honoring ``CAW_HOME`` (default ``~/.caw``)."""
    override = os.environ.get("CAW_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".caw"


def config_path() -> Path:
    """Path to the user config file (``~/.caw/config.json``)."""
    return caw_home() / "config.json"


def cache_path() -> Path:
    """Path to the cached remote defaults (``~/.caw/cache/models.json``)."""
    return caw_home() / "cache" / "models.json"


# --- canonicalization -------------------------------------------------------


def canonical_provider(name: str) -> str:
    """Map a provider name/alias to its canonical config key."""
    return _PROVIDER_ALIASES.get(name, name)


def _tier_str(tier: ModelTier | str) -> str:
    return tier.value if isinstance(tier, ModelTier) else str(tier)


# --- baked-in defaults ------------------------------------------------------

_baked_cache: dict[str, Any] | None = None


def _baked_defaults() -> dict[str, Any]:
    """Load the defaults bundled in the wheel (read once per process)."""
    global _baked_cache
    if _baked_cache is None:
        path = Path(__file__).parent / "defaults" / "models.json"
        try:
            _baked_cache = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read baked defaults at %s", path, exc_info=True)
            _baked_cache = {"models": {}}
    return _baked_cache


# --- user config ------------------------------------------------------------


def _load_user_config() -> dict[str, Any]:
    """Read ``~/.caw/config.json``; return ``{}`` if missing or malformed."""
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring malformed config at %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _save_user_config(data: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


# --- remote defaults --------------------------------------------------------


def defaults_url() -> str:
    """Resolve the remote defaults URL, or ``""`` when fetching is disabled."""
    v = os.environ.get("CAW_DEFAULTS_URL")
    if v is None:
        return DEFAULT_DEFAULTS_URL
    v = v.strip()
    if v.lower() in ("", "off", "none", "disable", "disabled", "0"):
        return ""
    return v


def _ttl_seconds() -> int:
    raw = os.environ.get("CAW_DEFAULTS_TTL")
    if not raw:
        return _DEFAULT_TTL_SECONDS
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_TTL_SECONDS


def _cached_defaults() -> dict[str, Any] | None:
    path = cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _cache_fresh() -> bool:
    path = cache_path()
    if not path.exists():
        return False
    ttl = _ttl_seconds()
    if ttl <= 0:
        return False
    return (time.time() - path.stat().st_mtime) < ttl


def _write_cache(data: dict[str, Any]) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except OSError:
        logger.debug("Could not write defaults cache at %s", path, exc_info=True)


def _fetch_remote() -> dict[str, Any] | None:
    """Fetch and cache the remote defaults; return ``None`` on any failure.

    Seam for tests — monkeypatch this to simulate the network.
    """
    url = defaults_url()
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SECONDS) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — network/parse errors are all non-fatal
        logger.debug("Remote defaults fetch failed for %s", url, exc_info=True)
        return None
    if not (isinstance(data, dict) and isinstance(data.get("models"), dict)):
        return None
    _write_cache(data)
    return data


# Per-process guard so an offline run does not retry the (timeout-bounded) fetch
# on every single ``get_model`` call.
_remote_attempted = False
_remote_result: dict[str, Any] | None = None


def _reset_remote_state() -> None:
    """Clear the per-process remote-fetch memo (used by tests)."""
    global _remote_attempted, _remote_result
    _remote_attempted = False
    _remote_result = None


def remote_defaults(force: bool = False) -> dict[str, Any] | None:
    """Return remote defaults (network at most once per process unless *force*)."""
    global _remote_attempted, _remote_result
    if not defaults_url():  # disabled — only ever consult the on-disk cache
        return _cached_defaults()
    if force:
        fetched = _fetch_remote()
        _remote_attempted = True
        if fetched is not None:
            _remote_result = fetched
            return fetched
        return _cached_defaults()
    if _remote_attempted:
        return _remote_result if _remote_result is not None else _cached_defaults()
    _remote_attempted = True
    if _cache_fresh():
        _remote_result = _cached_defaults()
        return _remote_result
    fetched = _fetch_remote()
    _remote_result = fetched
    return fetched if fetched is not None else _cached_defaults()


def refresh_defaults() -> dict[str, Any] | None:
    """Force a re-fetch of the remote defaults, updating the cache."""
    return remote_defaults(force=True)


# --- effective resolution ---------------------------------------------------


def _shipped_models() -> dict[str, dict[str, Any]]:
    """Baked defaults with remote/cached values layered on top (per provider/tier)."""
    merged: dict[str, dict[str, Any]] = {}
    for source in (_baked_defaults(), remote_defaults() or {}):
        for provider, tiers in (source.get("models") or {}).items():
            if isinstance(tiers, dict):
                merged.setdefault(provider, {}).update(tiers)
    return merged


def get_model(provider: str, tier: ModelTier | str) -> str | None:
    """Resolve the default model for *provider*/*tier* (see module docstring).

    Returns ``None`` when no model is configured at any layer (the provider then
    falls back to its own built-in default).
    """
    provider = canonical_provider(provider)
    tier_s = _tier_str(tier)

    env_name = _ENV_OVERRIDES.get((provider, tier_s))
    if env_name:
        env_val = os.environ.get(env_name)
        if env_val:
            return env_val

    user = _load_user_config().get("models", {})
    if isinstance(user, dict) and tier_s in user.get(provider, {}):
        model: str | None = user[provider][tier_s]
        return model

    shipped: str | None = _shipped_models().get(provider, {}).get(tier_s)
    return shipped


def set_user_model(provider: str, tier: ModelTier | str, model: str) -> None:
    """Persist a user override for *provider*/*tier* into ``~/.caw/config.json``."""
    provider = canonical_provider(provider)
    tier_s = _tier_str(tier)
    data = _load_user_config()
    models = data.setdefault("models", {})
    models.setdefault(provider, {})[tier_s] = model
    _save_user_config(data)


def unset_user_model(provider: str, tier: ModelTier | str) -> bool:
    """Remove a user override; return ``True`` if one was present."""
    provider = canonical_provider(provider)
    tier_s = _tier_str(tier)
    data = _load_user_config()
    models = data.get("models", {})
    prov = models.get(provider, {})
    if tier_s not in prov:
        return False
    del prov[tier_s]
    if not prov:
        models.pop(provider, None)
    if not models:
        data.pop("models", None)
    _save_user_config(data)
    return True


def describe_models() -> list[dict[str, Any]]:
    """Return the effective model for every known provider/tier, with provenance.

    Each row is ``{"provider", "tier", "model", "source"}`` where ``source`` is
    one of ``env:<VAR>`` / ``user`` / ``remote`` / ``baked`` / ``unset``.
    Powers ``caw config list``.
    """
    baked = _baked_defaults().get("models") or {}
    remote = (remote_defaults() or {}).get("models") or {}
    user = _load_user_config().get("models") or {}

    providers = sorted(set(baked) | set(remote) | set(user))
    rows: list[dict[str, Any]] = []
    for provider in providers:
        for tier in TIERS:
            env_name = _ENV_OVERRIDES.get((provider, tier))
            env_val = os.environ.get(env_name) if env_name else None
            if env_val:
                rows.append({"provider": provider, "tier": tier, "model": env_val, "source": f"env:{env_name}"})
            elif tier in user.get(provider, {}):
                rows.append({"provider": provider, "tier": tier, "model": user[provider][tier], "source": "user"})
            elif tier in remote.get(provider, {}):
                rows.append({"provider": provider, "tier": tier, "model": remote[provider][tier], "source": "remote"})
            elif tier in baked.get(provider, {}):
                rows.append({"provider": provider, "tier": tier, "model": baked[provider][tier], "source": "baked"})
            else:
                rows.append({"provider": provider, "tier": tier, "model": None, "source": "unset"})
    return rows
