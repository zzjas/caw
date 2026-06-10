# Models & tiers

You can pick a model two ways: a **concrete model string** (provider-specific) or an abstract
[`ModelTier`][caw.ModelTier] that each provider maps to its own model.

## Concrete model strings

Pass whatever the backend understands:

```python
from caw import Agent

agent = Agent(provider="claude_code", model="opus")
agent = Agent(provider="codex", model="gpt-5.5")
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
| `ModelTier.STRONGEST` | Best available model | `opus` | provider default |
| `ModelTier.FAST` | Cheapest / fastest | `claude-haiku-4-5` | `gpt-5.3-codex-spark` |

Because tiers re-resolve per provider, they're the right choice whenever you use a fallback
order. See [`examples/model_tiers.py`](https://github.com/zzjas/caw/blob/main/examples/model_tiers.py):

```python
--8<-- "examples/model_tiers.py"
```

## Configuring tier defaults

The model each tier maps to is **not** hardcoded — it's resolved from config that lives under
`~/.caw/` (override the base dir with `CAW_HOME`). Inspect and edit it with the `caw config` CLI:

```console
$ caw config list                              # effective model per provider/tier, with source
$ caw config set opencode strongest openai/gpt-5.5-turbo
$ caw config get opencode strongest
$ caw config unset opencode strongest          # revert to the shipped default
$ caw config path                              # where ~/.caw/config.json lives
$ caw config refresh                           # re-fetch the shipped defaults now
```

`caw config set` writes to `~/.caw/config.json`; only the keys you override are stored, and
everything else falls through to the shipped defaults.

For a given provider/tier, the model is resolved in this order (highest first):

1. The provider env var — `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL`,
   `OPENCODE_MODEL` / `OPENCODE_SMALL_FAST_MODEL`
2. **User config** `~/.caw/config.json` (what `caw config set` edits)
3. **Remote defaults** fetched from `CAW_DEFAULTS_URL` and cached under `~/.caw/cache/`
4. **Baked-in defaults** shipped in the wheel (the offline floor)

An explicit `model=` on the `Agent` (or `CAW_MODEL`) bypasses tier resolution entirely.

### Updatable shipped defaults

The shipped defaults are served from a JSON file in the repo
([`caw/defaults/models.json`](https://github.com/zzjas/caw/blob/main/caw/defaults/models.json)),
which is both bundled in the wheel and published at the default `CAW_DEFAULTS_URL`. caw fetches
it lazily and caches it for `CAW_DEFAULTS_TTL` seconds (default 24h). That means the default
models can be **updated without cutting a release** — edit that file on `main` and every install
picks it up within the TTL (or immediately with `caw config refresh`). Set `CAW_DEFAULTS_URL=off`
to disable network fetches and pin to the baked-in defaults.

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
