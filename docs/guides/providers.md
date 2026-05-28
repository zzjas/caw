# Providers

caw wraps three coding-agent CLIs behind one interface. A *provider* is the backend that
actually runs the model.

| Provider | CLI binary | Provider name(s) |
|----------|------------|------------------|
| Claude Code | `claude` | `claude_code`, `claude`, `cc` |
| Codex | `codex` | `codex` |
| opencode | `opencode` | `opencode` |

The names (and aliases) are what you pass as `provider=`. All three aliases for Claude Code
resolve to the same backend.

## Choosing a provider

There are three places to set the provider, in priority order:

```python
import os
from caw import Agent

# 1. Constructor (highest priority)
agent = Agent(provider="codex")

# 2. Environment variable
os.environ["CAW_PROVIDER"] = "codex"

# 3. At runtime, before starting a session
agent.set_provider("codex")
```

With no provider set anywhere, caw defaults to `claude_code`.

## Pinned vs. fallback

The `provider` argument accepts more than a single name:

- **A single name** (`provider="claude"`) — *pinned*. caw uses exactly that backend and
  surfaces its errors directly.
- **A list** (`provider=["claude", "codex", "opencode"]`) — a *fallback order*. caw selects
  the first installed provider and, on the first send, transparently moves to the next if one
  fails or is rate-limited.
- **`"auto"`** (or omitted) — use the global order from
  [`set_provider_order()`][caw.set_provider_order], else `CAW_PROVIDER`, else the default.

The list and `"auto"` forms are covered in detail in
[Auto-provider mode](auto-provider.md).

## Model selection across providers

A concrete model string (`model="opus"`) is provider-specific. To stay portable when you might
fall back to a different provider, prefer a [`ModelTier`](models-and-tiers.md):

```python
from caw import Agent, ModelTier

agent = Agent(provider=["claude", "codex"], model=ModelTier.STRONGEST)
```

Tiers are re-resolved per provider, so `STRONGEST`/`FAST` always map to that backend's best or
fastest model.

## Registering a custom provider

Providers are looked up in a registry. To add your own backend, subclass
[`Provider`][caw.Provider] / [`ProviderSession`][caw.ProviderSession] and register it:

```python
import caw

caw.register_provider("my_backend", MyProvider)
agent = caw.Agent(provider="my_backend")
```

See the [Providers API reference](../reference/api/providers.md) for the full abstract
interface a provider must implement (`start_session`, `resolve_model`,
`resolve_tool_restrictions`, health hooks, and resume support).
