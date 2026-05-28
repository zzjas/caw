# Installation

## Install caw

caw is on PyPI as `coding-agent-wrapper`. The recommended path is
[uv](https://docs.astral.sh/uv/) — it's fast, reproducible, and handles Python versions for
you:

```bash
uv add coding-agent-wrapper            # use as a library in a uv-managed project
uv tool install coding-agent-wrapper   # install just the CLI (caw, caw-traj) globally
```

Plain `pip` works too:

```bash
pip install coding-agent-wrapper
```

caw requires **Python 3.10+**. Import it as `caw`:

```python
import caw
from caw import Agent
```

## CLI prerequisites

caw is a *wrapper* — it drives the real coding-agent CLIs, so you need at least one of them
installed and authenticated on your `PATH`:

| Provider | CLI binary | Install docs |
|----------|------------|--------------|
| Claude Code | `claude` | <https://docs.claude.com/en/docs/claude-code> |
| Codex | `codex` | <https://github.com/openai/codex> |
| opencode | `opencode` | <https://github.com/sst/opencode> |

You only need the one(s) you intend to use. Check what caw can see with:

```bash
caw doctor
```

This prints whether each CLI is installed, where its binary lives, and what caw can tell about
its credentials — see [Provider health](../guides/health.md).

## Development install

From a checkout of the repo, sync with [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev
```

Run commands inside that environment with `uv run` (e.g. `uv run pytest`, `uv run ruff check
.`). If you'd rather use `pip`:

```bash
pip install -e '.[dev]'
```

### Building the docs locally

The documentation site is built with [MkDocs](https://www.mkdocs.org/) + Material. Sync the
`docs` extra and serve it:

```bash
uv sync --extra docs
uv run mkdocs serve
```

Then open <http://127.0.0.1:8000>. Use `uv run mkdocs build --strict` to catch broken links
and missing references the way CI does.

## Next steps

- [Quickstart](quickstart.md) — your first agent in 60 seconds.
- [Concepts](concepts.md) — the `Agent` / `Session` / `Trajectory` mental model.
