# Auto-provider mode

Don't want to hard-code one provider? Give caw a **fallback order** and let it use whatever is
available at runtime. caw selects the first *installed* provider and, on the first send,
transparently moves to the next one if that provider fails (CLI missing, auth expired) or is
rate-limited — no exception handling or provider-picking on your side.

```python
import caw
from caw import Agent

caw.set_provider_order(["claude", "codex", "opencode"])  # set once, globally

agent = Agent(provider="auto")            # uses the global order
traj = agent.completion("Explain this repo")
print(f"[{traj.agent}] {traj.result}")    # whichever provider handled it
```

## Where the order comes from

In priority order (highest first):

```python
Agent(provider=["claude", "codex"])              # explicit per-agent order
caw.set_provider_order([...])                     # global default, used by provider="auto"
os.environ["CAW_PROVIDER"] = "claude,codex,opencode"  # env var, comma list
```

A single name (`provider="claude"`) stays **pinned** — no fallback. Use a list or `"auto"` to
opt into fallback.

## How fallback works

1. **Selection** is a fast, no-network check: caw picks the first provider in the order whose
   CLI binary is installed. This is what `agent.provider` reports.
2. **On the first `send()`**, if the chosen provider raises (missing CLI, auth error) or
   reports a usage limit, caw silently builds the next provider's session and retries.
3. **Once a provider produces the first turn, the session is committed to it.** Conversation
   context can't move across CLIs mid-stream, so any later failure propagates normally.

!!! tip "Prefer a `ModelTier` in auto mode"
    Use a [`ModelTier`](models-and-tiers.md) (or no model) rather than a concrete model
    string. Tiers are re-resolved per provider, so model selection stays portable across the
    fallback. A bare concrete model string is **dropped** when falling back to a different
    provider — it would be meaningless to the others.

## A model per provider in the order

To pin a *specific* model to each provider in the order, attach it to
`set_provider_order` — as `(name, model)` tuples or a `models=` mapping. Each value may be a
concrete string or a `ModelTier`. Because the model is bound to its provider, it is honored even
when that provider is reached as a fallback (unlike a bare Agent-level string):

```python
import caw
from caw import Agent, ModelTier

caw.set_provider_order([
    ("claude", ModelTier.STRONGEST),   # re-resolved via the claude tier config
    ("codex", "gpt-5.5"),              # concrete, bound to codex
    ("opencode", "openai/gpt-5.5"),
])
# Equivalent: caw.set_provider_order(["claude", "codex"], models={"codex": "gpt-5.5"})

caw.get_provider_models()             # {'claude': <ModelTier.STRONGEST>, 'codex': 'gpt-5.5', ...}

Agent(provider="auto")                # each provider uses its attached model
```

A provider's order-model applies only when the `Agent` sets no `model` of its own — an explicit
`model=` (or `CAW_MODEL`) on the `Agent` always wins.

## Inspecting the selection

Pair auto-provider with [`check_providers()`](health.md) to see what's installed before you
commit:

```python
from caw import Agent, check_providers

for h in check_providers(["claude", "codex", "opencode"]):
    print("✓" if h.installed else "✗", h.provider)

print("selected:", Agent(provider="auto").provider.name)
```

## Full example

[`examples/auto_provider.py`](https://github.com/zzjas/caw/blob/main/examples/auto_provider.py):

```python
--8<-- "examples/auto_provider.py"
```
