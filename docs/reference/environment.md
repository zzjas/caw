# Environment variables

caw can be configured globally via environment variables, so you can change behavior without
touching code. Each is used as a fallback when the corresponding value isn't set explicitly on
the `Agent` constructor or a method call.

| Variable | Purpose | Example |
|----------|---------|---------|
| `CAW_PROVIDER` | Default provider, or a comma-separated [fallback order](../guides/auto-provider.md) | `claude_code` · `claude,codex,opencode` |
| `CAW_MODEL` | Default model name | `opus` · `gpt-5.5` |
| `CAW_EFFORT` | Default reasoning effort | `high` · `medium` · `low` |
| `CAW_AUTOWAIT` | Auto-wait on usage limits (on by default) | `0` / `false` to disable |
| `CAW_LOG` | Console [display mode](../guides/display-and-logging.md) | `full` · `short` · `result` · `off` |
| `CAW_AUTH_DIR` | Relocate the [auth staging dir](../guides/docker-credentials.md) (default `~/.caw/auth/`) | `/path/to/auth` |

## Precedence

For the provider, model, and reasoning settings, the order is:

1. Explicit argument to `Agent(...)` or a setter (`set_provider`, `set_model`, …)
2. The environment variable above
3. caw's built-in default (`claude_code`, no model, provider default effort)

`CAW_PROVIDER` may be a single name (pinned) or a comma-separated list (a fallback order used
when `provider="auto"` or no provider is given). See
[Auto-provider mode](../guides/auto-provider.md).
