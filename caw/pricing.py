"""Token-based cost computation with user-editable pricing overrides.

Per-token prices for each ``(agent, model)`` pair are resolved with this
precedence (highest first):

1. **User config** ``~/.caw/config.json`` under the ``"pricing"`` key — what
   ``caw pricing set`` edits.  Overrides merge field-by-field onto the shipped
   rates, so you can bump just ``output`` and keep ``input``/``cached_input``.
2. **Baked-in defaults** — ``caw/defaults/pricing.json`` shipped in the wheel.

This mirrors how :mod:`caw.config` resolves default models: a sensible table is
bundled with the package, but every user can override it under ``~/.caw``
without editing the installed files.

Prices are in USD per 1 million tokens, keyed by agent then model, e.g.
``{"opencode": {"openai/gpt-5.5": {"input": 5.0, "cached_input": 0.5,
"output": 30.0}}}``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from caw import config
from caw.models import UsageStats

logger = logging.getLogger(__name__)

# Rate fields recognised in a pricing entry (USD per 1M tokens).
_RATE_FIELDS: tuple[str, ...] = ("input", "cached_input", "output")

_baked_cache: dict[str, Any] | None = None


# --- pricing tables ---------------------------------------------------------


def _baked_pricing() -> dict[str, Any]:
    """Load the pricing table bundled in the wheel (read once per process)."""
    global _baked_cache
    if _baked_cache is None:
        path = Path(__file__).parent / "defaults" / "pricing.json"
        try:
            _baked_cache = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read baked pricing at %s", path, exc_info=True)
            _baked_cache = {}
    return _baked_cache


def _user_pricing() -> dict[str, Any]:
    """Pricing overrides from ``~/.caw/config.json`` (``{}`` if absent/malformed)."""
    pricing = config._load_user_config().get("pricing", {})
    return pricing if isinstance(pricing, dict) else {}


def _agent_tables(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Agent → models sub-tables, skipping metadata keys like ``_comment``."""
    return {agent: models for agent, models in table.items() if isinstance(models, dict)}


# --- effective resolution ---------------------------------------------------


def get_pricing(agent: str, model: str) -> dict[str, float]:
    """Effective per-1M-token rates for *agent*/*model* (baked overlaid by user).

    Returns only the rate fields that are actually configured, as floats; an
    empty dict means no pricing is known (cost then computes as ``0``).
    """
    agent = config.canonical_provider(agent)
    baked = _baked_pricing().get(agent, {}).get(model, {})
    user = _user_pricing().get(agent, {}).get(model, {})
    merged = {**baked, **(user if isinstance(user, dict) else {})}
    return {k: float(v) for k, v in merged.items() if k in _RATE_FIELDS and isinstance(v, (int, float))}


def compute_cost(agent: str, model: str, usage: UsageStats) -> float:
    """Compute cost in USD from token counts and the effective pricing config."""
    pricing = get_pricing(agent, model)
    cost = (
        usage.input_tokens * pricing.get("input", 0.0)
        + usage.cache_read_tokens * pricing.get("cached_input", 0.0)
        + usage.output_tokens * pricing.get("output", 0.0)
    ) / 1_000_000
    return cost


# --- user overrides ---------------------------------------------------------


def set_user_pricing(agent: str, model: str, rates: dict[str, float]) -> None:
    """Merge *rates* into the user override for *agent*/*model* in the config.

    Only keys in :data:`_RATE_FIELDS` with non-``None`` values are written, and
    they merge onto any existing override so partial updates keep prior fields.
    """
    agent = config.canonical_provider(agent)
    data = config._load_user_config()
    pricing = data.setdefault("pricing", {})
    entry = pricing.setdefault(agent, {}).setdefault(model, {})
    entry.update({k: float(v) for k, v in rates.items() if k in _RATE_FIELDS and v is not None})
    config._save_user_config(data)


def unset_user_pricing(agent: str, model: str) -> bool:
    """Remove a user pricing override; return ``True`` if one was present."""
    agent = config.canonical_provider(agent)
    data = config._load_user_config()
    pricing = data.get("pricing", {})
    agent_entry = pricing.get(agent, {})
    if model not in agent_entry:
        return False
    del agent_entry[model]
    if not agent_entry:
        pricing.pop(agent, None)
    if not pricing:
        data.pop("pricing", None)
    config._save_user_config(data)
    return True


def describe_pricing() -> list[dict[str, Any]]:
    """Return effective rates for every known agent/model, with provenance.

    Each row is ``{"agent", "model", "input", "cached_input", "output",
    "source"}`` where ``source`` is ``user`` (any field overridden) or
    ``baked``.  Powers ``caw pricing list``.
    """
    baked = _agent_tables(_baked_pricing())
    user = _agent_tables(_user_pricing())

    rows: list[dict[str, Any]] = []
    for agent in sorted(set(baked) | set(user)):
        models = sorted(set(baked.get(agent, {})) | set(user.get(agent, {})))
        for model in models:
            baked_entry = baked.get(agent, {}).get(model, {})
            user_entry = user.get(agent, {}).get(model, {})
            merged = {**baked_entry, **(user_entry if isinstance(user_entry, dict) else {})}
            rows.append(
                {
                    "agent": agent,
                    "model": model,
                    "input": merged.get("input"),
                    "cached_input": merged.get("cached_input"),
                    "output": merged.get("output"),
                    "source": "user" if user_entry else "baked",
                }
            )
    return rows
