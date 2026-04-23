# caw auth — Credential Management for Docker Containers

Coding agents store OAuth credentials in home directory files (e.g., `~/.claude/.credentials.json`). When running agents inside Docker containers, token refresh creates new tokens (OAuth rotation), invalidating the host's tokens.

`caw auth` solves this **without modifying host files**: it bind-mounts the host credentials directly into the container, and runs an inotify-based guard that syncs the container user's home copy with the bind-mounted host file in both directions.

## How it works

```
HOST:                                         CONTAINER:

~/.claude/.credentials.json  ←—docker bind—→  /tmp/caw_auth/claude/credentials.json
    (untouched, real file)                         ↓ copy + inotify sync
                                               /home/playground/.claude/.credentials.json
```

`~/.caw/auth/` only holds things CAW legitimately owns: the manifest, the container setup script, and cleaned/stripped configs. Credentials stay at their original host paths.

## Usage

```bash
caw auth setup                        # snapshot configs + write manifest/setup script
caw auth setup --agents claude codex  # specific agents only

caw auth status                       # token expiry, last modified, mount flags

docker run $(caw auth docker-flags) -v ./project:/work my-image

caw auth teardown                     # rm -rf ~/.caw/auth/  (host files never touched)
```

## Commands

### `caw auth setup`

Reads credentials and configs from the host, validates them, writes cleaned configs and a credential snapshot into `~/.caw/auth/`, and generates `manifest.json` + `setup-container.sh`. Host credential files are **read but never modified**.

- Credential files (tokens, OAuth) — `strategy: bind`. Bind-mounted from the host into the container at run time; the container-side guard copies them to the user's home and keeps them in sync.
- Config files (.claude.json, config.toml) — `strategy: copy`. Cleaned/stripped for containers and shipped in the staging directory.

### `caw auth teardown`

Removes `~/.caw/auth/`. Host credential files are never involved.

### `caw auth status`

Shows a table with each managed file, where its source of truth lives (host for bind, staged for copy), last modified time, and token expiry for credential files. Credential freshness is read from the host file directly.

### `caw auth docker-flags`

Emits one directory mount for the staging area plus one file mount per credential:

```bash
$ caw auth docker-flags
-v /home/user/.caw/auth:/tmp/caw_auth:rw \
-v /home/user/.claude/.credentials.json:/tmp/caw_auth/claude/credentials.json:rw \
-v /home/user/.codex/auth.json:/tmp/caw_auth/codex/auth.json:rw
```

Command substitution (`$(caw auth docker-flags)`) expands these into separate `docker run` arguments.

## Container setup

The generated `setup-container.sh` runs inside the container (called from your entrypoint). It reads `manifest.json`, copies credentials and configs into the container user's home, and starts a bidirectional inotify guard for credential sync.

```bash
# In your entrypoint.sh:
if [ -f /tmp/caw_auth/setup-container.sh ]; then
    /tmp/caw_auth/setup-container.sh /tmp/caw_auth /home/playground playground
fi
```

The guard runs as root and uses plain `cp` (no `--preserve`, no `chown` on the mount side), so writes back to the host file preserve the host user's uid/gid/mode on the real inode. Requires `jq` in the container image; `inotify-tools` is installed automatically if not present.

## Directory structure

```
~/.caw/auth/
├── manifest.json              # file map + metadata (records strategy per file)
├── setup-container.sh         # POSIX script for container setup
├── claude/
│   ├── credentials.json       # snapshot (bind-mounted over at container run)
│   └── config.json            # cleaned .claude.json (copied)
└── codex/
    ├── auth.json              # snapshot (bind-mounted over at container run)
    └── config.toml            # cleaned config (copied)
```

The credential snapshots exist so Docker has a target path to overlay with the host file. The staged bytes are never read at container run time — the bind mount supersedes them.

## Supported agents

| Agent | Credential files | Config files |
|-------|-----------------|--------------|
| Claude Code | `.claude/.credentials.json` | `.claude.json` (stripped to essential keys) |
| Codex | `.codex/auth.json` | `.codex/config.toml` (local trust removed) |

## Programmatic API

```python
from caw.auth import setup, teardown, get_status, get_docker_flags

setup(agents=["claude"])
statuses = get_status()
flags = get_docker_flags()
teardown()
```

See [`examples/auth.py`](../../examples/auth.py) for a full example.

## Known limitations

- **OAuth token rotation**: a refresh returns a new refresh token, invalidating the old one. If two processes refresh simultaneously, one gets an invalid token. Don't run the same agent identity in two places at once.
- **Atomic rewrites**: if an agent refreshes by writing a temp file and `rename(2)`-ing it over the credential, a single-file bind mount detaches from the new inode. If this becomes a real problem for a given agent, switch that agent's bind to a directory bind (mount the parent directory instead), which survives renames.
