# Docker credentials

Coding agents store OAuth credentials in home-directory files (e.g.
`~/.claude/.credentials.json`). When you run an agent **inside a Docker container**, token
refresh creates new tokens (OAuth rotation) and invalidates the host's tokens. `caw auth`
solves this **without modifying host files**: it bind-mounts the host credentials into the
container and runs an inotify-based guard that keeps the container's copy and the bind-mounted
host file in sync, both directions.

```bash
caw auth setup                        # snapshot configs, write mount manifest
caw auth status                       # token expiry, last modified, mount flags
docker run $(caw auth docker-flags) -v ./project:/work my-image
caw auth teardown                     # rm -rf ~/.caw/auth/  (host files untouched)
```

## How it works

```
HOST:                                         CONTAINER:

~/.claude/.credentials.json  ←—docker bind—→  /tmp/caw_auth/claude/credentials.json
    (untouched, real file)                         ↓ copy + inotify sync
                                               /home/playground/.claude/.credentials.json
```

`~/.caw/auth/` only holds things caw legitimately owns: the manifest, the container setup
script, and cleaned/stripped configs. Credentials stay at their original host paths.

## Commands

### `caw auth setup`

Reads credentials and configs from the host, validates them, writes cleaned configs and a
credential snapshot into `~/.caw/auth/`, and generates `manifest.json` + `setup-container.sh`.
Host credential files are **read but never modified**.

- **Credential files** (tokens, OAuth) — `strategy: bind`. Bind-mounted from the host into the
  container at run time; the container-side guard copies them to the user's home and keeps
  them in sync.
- **Config files** (`.claude.json`, `config.toml`) — `strategy: copy`. Cleaned/stripped for
  containers and shipped in the staging directory.

```bash
caw auth setup                        # all detected agents
caw auth setup --agents claude codex  # specific agents only
```

### `caw auth status`

Shows a table with each managed file, where its source of truth lives (host for bind, staged
for copy), last-modified time, and token expiry for credential files. Credential freshness is
read from the host file directly.

### `caw auth docker-flags`

Emits one directory mount for the staging area plus one file mount per credential:

```bash
$ caw auth docker-flags
-v /home/user/.caw/auth:/tmp/caw_auth:rw \
-v /home/user/.claude/.credentials.json:/tmp/caw_auth/claude/credentials.json:rw \
-v /home/user/.codex/auth.json:/tmp/caw_auth/codex/auth.json:rw
```

Command substitution (`$(caw auth docker-flags)`) expands these into separate `docker run`
arguments.

### `caw auth teardown`

Removes `~/.caw/auth/`. Host credential files are never involved. It refuses to run if a host
credential file is still a symlink into the auth dir (leftover from caw's old symlink-based
design); pass `--force` to override — but you'll have to re-authenticate every agent.

## Container setup

The generated `setup-container.sh` runs inside the container (called from your entrypoint). It
reads `manifest.json`, copies credentials and configs into the container user's home, and
starts a bidirectional inotify guard for credential sync.

```bash
# In your entrypoint.sh:
if [ -f /tmp/caw_auth/setup-container.sh ]; then
    /tmp/caw_auth/setup-container.sh /tmp/caw_auth /home/playground playground
fi
```

The guard runs as root and uses plain `cp` (no `--preserve`, no `chown` on the mount side), so
writes back to the host file preserve the host user's uid/gid/mode on the real inode. Requires
`jq` in the container image; `inotify-tools` is installed automatically if not present.

## Supported agents

| Agent | Credential files | Config files |
|-------|------------------|--------------|
| Claude Code | `.claude/.credentials.json` | `.claude.json` (stripped to essential keys) |
| Codex | `.codex/auth.json` | `.codex/config.toml` (local trust removed) |

## Programmatic API

The same operations are available in Python:

```python
from caw.auth import setup, teardown, get_status, get_docker_flags

setup(agents=["claude"])
statuses = get_status()
flags = get_docker_flags()
teardown()
```

These are also re-exported at the top level as
[`auth_setup`][caw.auth_setup], [`auth_get_status`][caw.auth_get_status], and
[`auth_get_docker_flags`][caw.auth_get_docker_flags]. Full example —
[`examples/auth.py`](https://github.com/zzjas/caw/blob/main/examples/auth.py):

```python
--8<-- "examples/auth.py"
```

See the [Auth API reference](../reference/api/auth.md) for full signatures.

## Known limitations

- **OAuth token rotation.** A refresh returns a new refresh token, invalidating the old one.
  If two processes refresh simultaneously, one gets an invalid token. Don't run the same agent
  identity in two places at once.
- **Atomic rewrites.** If an agent refreshes by writing a temp file and `rename(2)`-ing it
  over the credential, a single-file bind mount detaches from the new inode. If that becomes a
  problem for a given agent, switch its bind to a directory bind (mount the parent directory),
  which survives renames.
