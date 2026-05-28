"""Provider health/availability signals.

This module intentionally reports *raw signals* and forms no verdict on what
counts as "available" — callers compose their own predicate from the fields
(e.g. ``h.installed and not (h.auth and h.auth.token_expired)``).

Two depths of check:

* **fast** (default): is the CLI binary installed, and what can we cheaply
  introspect about credentials?  No network, no token cost.
* **live** (``live=True``): additionally round-trips a minimal prompt via
  `Provider.check_limit` to confirm the provider actually responds and
  whether it is currently rate-limited.  Costs one probe request.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class AuthSignal:
    """Best-effort, non-authoritative view of a provider's credentials.

    Every field is a raw signal; ``None`` means "could not determine" rather
    than a negative result.  Detection is intentionally cheap and may miss
    valid setups (e.g. credentials supplied by an env var we don't know about),
    so callers should treat a falsy ``present`` as a hint, not a verdict.
    """

    present: bool  # a credential source (file or known env var) was detected
    detail: str  # human-readable summary
    credentials_path: str | None = None  # primary credential file we looked at
    token_expires_at: datetime | None = None  # parsed OAuth expiry, if readable
    token_expired: bool | None = None  # None = unknown / not an OAuth token


@dataclass
class ProviderHealth:
    """Raw health signals for a single provider. No 'available' verdict."""

    provider: str  # canonical provider name (e.g. "claude_code")
    installed: bool  # CLI binary found on PATH (or a known fallback location)
    binary_path: str | None = None  # resolved path to the CLI, or None
    auth: AuthSignal | None = None  # best-effort credential introspection

    # Populated only when ``check_health(live=True)`` runs the probe:
    probed: bool = False  # whether a live round-trip was attempted
    rate_limited: bool | None = None  # True/False from the probe; None if not probed
    wait_minutes: int | None = None  # estimated minutes until the limit resets
    error: str | None = None  # exception text if the live probe failed


# ---------------------------------------------------------------------------
# Auth introspection helpers (reuse caw.auth.providers' path knowledge)
# ---------------------------------------------------------------------------


def _claude_token_expiry(path: Path) -> tuple[datetime | None, bool | None]:
    """Parse Claude's OAuth token expiry from ``.credentials.json``."""
    try:
        if not path.exists():
            return None, None
        with open(path) as f:
            creds = json.load(f)
        ts = creds.get("claudeAiOauth", {}).get("expiresAt")
        if not ts:
            return None, None
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        return dt, dt < datetime.now(timezone.utc)
    except Exception:
        return None, None


def _auth_signal(
    agent_name: str,
    env_vars: tuple[str, ...] = (),
    *,
    claude_expiry: bool = False,
) -> AuthSignal:
    """Build an `AuthSignal` for *agent_name*.

    Reuses the credential-location knowledge in `caw.auth.providers`
    (``validate`` for presence, ``describe`` for a summary) and additionally
    treats any set ``env_vars`` as a credential source.
    """
    from caw.auth.providers import PROVIDERS

    home = Path.home()
    ap = PROVIDERS.get(agent_name)
    missing = ap.validate(home) if ap else None
    file_present = ap is not None and not missing
    env_hit = next((v for v in env_vars if os.environ.get(v)), None)
    present = file_present or env_hit is not None

    parts: list[str] = []
    if file_present:
        parts.append(ap.describe(home))  # type: ignore[union-attr]
    if env_hit:
        parts.append(f"${env_hit} set")
    if not present:
        parts.append(f"no credentials ({missing[0]} missing)" if missing else "no credentials found")

    cred_path: str | None = None
    expires_at: datetime | None = None
    expired: bool | None = None
    if claude_expiry:
        cred_path = str(home / ".claude" / ".credentials.json")
        expires_at, expired = _claude_token_expiry(Path(cred_path))

    return AuthSignal(
        present=present,
        detail="; ".join(p for p in parts if p),
        credentials_path=cred_path,
        token_expires_at=expires_at,
        token_expired=expired,
    )


def claude_auth_signal() -> AuthSignal:
    return _auth_signal("claude", ("ANTHROPIC_API_KEY",), claude_expiry=True)


def codex_auth_signal() -> AuthSignal:
    return _auth_signal("codex", ("OPENAI_API_KEY",))


def opencode_auth_signal() -> AuthSignal:
    return _auth_signal("opencode")


# ---------------------------------------------------------------------------
# Multi-provider sweep
# ---------------------------------------------------------------------------


def check_providers(
    names: list[str] | None = None,
    *,
    live: bool = False,
    model: str | None = None,
) -> list[ProviderHealth]:
    """Return `ProviderHealth` for each registered provider.

    Args:
        names: Provider names/aliases to check, or ``None`` for every
            registered provider. Aliases pointing at the same provider class
            (e.g. ``claude``/``cc``/``claude_code``) collapse to one result.
        live: If True, run the live probe per provider (costs a request each).
        model: Optional model string passed through to the live probe.

    Returns:
        One `ProviderHealth` per distinct provider, in the order given
        (or registration order when ``names`` is None).
    """
    from caw.agent import _PROVIDER_REGISTRY

    keys = names if names is not None else list(_PROVIDER_REGISTRY.keys())
    results: list[ProviderHealth] = []
    seen: set[type] = set()
    for key in keys:
        cls = _PROVIDER_REGISTRY.get(key)
        if cls is None:
            available = list(_PROVIDER_REGISTRY.keys())
            raise ValueError(f"Unknown provider {key!r}. Available: {available}")
        if cls in seen:
            continue
        seen.add(cls)
        results.append(cls().check_health(live=live, model=model))
    return results
