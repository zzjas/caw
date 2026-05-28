# caw auth — credential management for Docker containers

`caw auth` keeps coding-agent OAuth credentials in sync between your host and Docker
containers, **without modifying host files**: it bind-mounts the host credentials into the
container and runs an inotify-based guard that syncs both directions.

```bash
caw auth setup                        # snapshot configs, write mount manifest
caw auth status                       # token expiry, last modified, mount flags
docker run $(caw auth docker-flags) -v ./project:/work my-image
caw auth teardown                     # rm -rf ~/.caw/auth/  (host files untouched)
```

📖 **Full documentation** — how it works, container setup, supported agents, the programmatic
API, and known limitations — lives in the docs:

**<https://zzjas.github.io/caw/guides/docker-credentials/>**

See also [`examples/auth.py`](../../examples/auth.py) for the programmatic API.
