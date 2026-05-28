# Provider health

Check whether a provider is set up correctly — without committing to using it. caw reports
**raw signals** and forms no "available" verdict, so you write your own predicate. The default
check is fast and free; a live probe round-trips a tiny request to confirm the provider
responds and isn't rate-limited.

```python
from caw import Agent, check_providers

for h in check_providers():               # fast: no network, no token cost
    print(h.provider, h.installed, h.binary_path,
          h.auth.detail if h.auth else None)
```

## Compose your own "available"

Because caw gives you signals rather than a verdict, you decide what "usable" means:

```python
usable = [h.provider for h in check_providers()
          if h.installed and not (h.auth and h.auth.token_expired)]
```

## Two depths of check

- **Fast (default).** Is the CLI installed, where is its binary, and what can caw cheaply tell
  about its credentials? No network, no token cost — safe at startup.
- **Live (`live=True`).** Additionally round-trips a minimal prompt to confirm the provider
  actually responds and whether it's currently rate-limited. **Costs one probe request per
  provider.**

```python
h = Agent(provider="codex").check_health(live=True)
if h.rate_limited:
    print(f"codex rate-limited, ~{h.wait_minutes}m until reset")
```

## What `ProviderHealth` exposes

[`ProviderHealth`][caw.ProviderHealth] carries:

| Field | Meaning |
|-------|---------|
| `provider` | Canonical provider name |
| `installed` | CLI binary found on `PATH` (or a known fallback) |
| `binary_path` | Resolved path to the CLI, or `None` |
| `auth` | An [`AuthSignal`][caw.AuthSignal], or `None` if not introspectable |
| `probed` | Whether a live round-trip was attempted |
| `rate_limited` | From the probe; `None` if not probed |
| `wait_minutes` | Estimated minutes until the limit resets |
| `error` | Exception text if the live probe failed |

[`AuthSignal`][caw.AuthSignal] adds `present`, `detail`, `credentials_path`,
`token_expires_at`, and `token_expired`. Every field is a *signal* — `None` means "couldn't
determine", not a negative result — so treat a falsy `present` as a hint, not a verdict.

## From the CLI

`caw doctor` prints the same signals as a table:

```bash
caw doctor            # fast: installed + credential signals (no token cost)
caw doctor --live     # also probe each provider (costs one request each)
```

## Full example

[`examples/health.py`](https://github.com/zzjas/caw/blob/main/examples/health.py):

```python
--8<-- "examples/health.py"
```
