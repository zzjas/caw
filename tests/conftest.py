"""Shared pytest fixtures.

Global safety net: a test must never write to the developer's real
``~/.caw/auth``. The auth tests call ``caw.auth.setup()`` which, when given no
explicit ``dest_dir``, writes to ``default_auth_dir()``. That helper honors the
``CAW_AUTH_DIR`` env var *before* falling back to ``Path.home()/.caw/auth``, so
pointing ``CAW_AUTH_DIR`` at a throwaway dir isolates every default-dir write
for the whole suite.

We deliberately override ``CAW_AUTH_DIR`` and NOT ``HOME``: the live tests
(``test_resume``, ``test_usage``, ``test_storage``) spawn a real ``claude``
process that reads credentials from ``~/.claude`` via ``HOME``. Redirecting
``HOME`` would strip their auth and make them fail with zero turns/tokens.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_caw_auth_dir(tmp_path_factory, monkeypatch):
    """Point CAW_AUTH_DIR at a throwaway dir so no test writes to the real ~/.caw/auth."""
    auth_dir = tmp_path_factory.mktemp("caw_auth")
    monkeypatch.setenv("CAW_AUTH_DIR", str(auth_dir))
    return auth_dir


@pytest.fixture(autouse=True)
def _isolate_caw_home(tmp_path_factory, monkeypatch):
    """Isolate model config: point CAW_HOME at a throwaway dir and disable remote
    defaults fetches, so model resolution is deterministic (baked defaults only)
    and no test reads/writes the developer's real ~/.caw/config.json."""
    import caw.config as _config

    home = tmp_path_factory.mktemp("caw_home")
    monkeypatch.setenv("CAW_HOME", str(home))
    monkeypatch.setenv("CAW_DEFAULTS_URL", "off")
    _config._reset_remote_state()
    yield home
    _config._reset_remote_state()
