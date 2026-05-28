# Changelog

caw is in **0.1.x alpha**. The authoritative, per-release notes live on GitHub:

[**Releases on GitHub →**](https://github.com/zzjas/caw/releases)

Install a specific version with:

```bash
pip install coding-agent-wrapper==0.1.6
```

## Recent highlights

- **Auto-provider mode** — give caw a [fallback order](../guides/auto-provider.md) and it
  selects the first installed provider, transparently moving to the next on a first-send
  failure or rate limit.
- **Provider health checks** — [`check_providers()`](../guides/health.md) and `caw doctor`
  report raw availability/credential signals, with an optional live probe.
- **Cross-process resuming** — self-contained [`resume_handle`](../guides/resuming.md)s that
  work with or without a `data_dir`.

Versioned documentation isn't published yet (deferred until 1.0); these docs track `main`.
