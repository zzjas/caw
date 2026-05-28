# Models & tiers

You can pick a model two ways: a **concrete model string** (provider-specific) or an abstract
[`ModelTier`][caw.ModelTier] that each provider maps to its own model.

## Concrete model strings

Pass whatever the backend understands:

```python
from caw import Agent

agent = Agent(provider="claude_code", model="opus")
agent = Agent(provider="codex", model="gpt-5.2-codex")
```

A concrete string is tied to one provider. In an [auto-provider](auto-provider.md) order it is
dropped on fallback (the next provider wouldn't recognize it).

## Model tiers (portable)

[`ModelTier`][caw.ModelTier] expresses intent — "give me the strongest" or "give me the
fast/cheap one" — and each provider resolves it to a concrete model:

```python
from caw import Agent, ModelTier

agent = Agent(model=ModelTier.STRONGEST)  # provider picks its best model
agent = Agent(model=ModelTier.FAST)       # provider picks its fast model
```

| Tier | Meaning | Example (Claude Code) | Example (Codex) |
|------|---------|-----------------------|-----------------|
| `ModelTier.STRONGEST` | Best available model | `opus` | `gpt-5.2-codex` |
| `ModelTier.FAST` | Cheapest / fastest | `claude-haiku-4-5` | `gpt-5.3-codex-spark` |

Because tiers re-resolve per provider, they're the right choice whenever you use a fallback
order. See [`examples/model_tiers.py`](https://github.com/zzjas/caw/blob/main/examples/model_tiers.py):

```python
--8<-- "examples/model_tiers.py"
```

## Reasoning effort

Set the reasoning budget with `reasoning=` (`"high"`, `"medium"`, `"low"`) at construction or
later:

```python
agent = Agent(model="opus", reasoning="high")
agent.set_reasoning("medium")
```

Both model and reasoning have environment-variable fallbacks — `CAW_MODEL` and `CAW_EFFORT` —
so you can configure them without touching code. See
[Environment variables](../reference/environment.md).

## Asking a sub-task to use a cheaper model

You can also steer the *agent itself* to use a cheaper model for exploratory sub-steps, as in
[`examples/haiku.py`](https://github.com/zzjas/caw/blob/main/examples/haiku.py):

```python
--8<-- "examples/haiku.py"
```
