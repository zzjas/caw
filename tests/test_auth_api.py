"""Tests for the programmatic auth API (setup, get_status, get_docker_flags)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from caw.auth import setup, get_docker_flags, get_status
from caw.auth.manifest import Manifest
from caw.auth.status import AuthFileStatus


# ---------------------------------------------------------------------------
# Helpers — build a fake home directory with credential stubs
# ---------------------------------------------------------------------------


def _make_fake_home(tmp_path: Path) -> Path:
    """Create a fake home dir with minimal Claude credentials."""
    home = tmp_path / "fake_home"
    home.mkdir()

    # .claude.json
    claude_json = {
        "oauthAccount": {"emailAddress": "test@example.com", "organizationName": "TestOrg"},
        "userID": "user123",
        "hasCompletedOnboarding": True,
        "projects": {"/some/path": {"allowedTools": []}},
    }
    (home / ".claude.json").write_text(json.dumps(claude_json))

    # .claude/.credentials.json
    claude_dir = home / ".claude"
    claude_dir.mkdir()
    creds = {"claudeAiOauth": {"accessToken": "tok123", "expiresAt": 9999999999000}}
    (claude_dir / ".credentials.json").write_text(json.dumps(creds))

    return home


# ---------------------------------------------------------------------------
# setup() with custom dest_dir
# ---------------------------------------------------------------------------


class TestSetupCustomDir:
    def test_setup_to_custom_dir(self, tmp_path: Path):
        """setup() writes auth files to dest_dir instead of ~/.caw/auth."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"

        result = setup(
            agents=["claude"],
            source_home=str(home),
            dest_dir=dest,
        )

        assert result == dest
        assert dest.exists()
        assert (dest / "manifest.json").exists()
        assert (dest / "setup-container.sh").exists()
        assert (dest / "claude" / "credentials.json").exists()
        assert (dest / "claude" / "config.json").exists()

    def test_setup_manifest_has_correct_home(self, tmp_path: Path):
        """The manifest records the source home, not the dest dir."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"

        setup(agents=["claude"], source_home=str(home), dest_dir=dest)

        manifest = Manifest.load(dest / "manifest.json")
        assert manifest.host_home == str(home)
        assert "claude" in manifest.agents

    def test_setup_default_dir_is_caw_auth(self, tmp_path: Path, monkeypatch):
        """When dest_dir is None, setup uses ~/.caw/auth."""
        home = _make_fake_home(tmp_path)
        caw_auth = tmp_path / "default_caw" / "auth"

        # Monkeypatch AUTH_DIR so we don't touch the real filesystem
        import caw.auth.collector as collector_mod

        monkeypatch.setattr(collector_mod, "AUTH_DIR", caw_auth)

        result = setup(agents=["claude"], source_home=str(home))
        assert result == caw_auth
        assert caw_auth.exists()

    def test_setup_overwrites_existing(self, tmp_path: Path):
        """Re-collecting to the same dest_dir replaces old files."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"

        setup(agents=["claude"], source_home=str(home), dest_dir=dest)
        first_manifest = (dest / "manifest.json").read_text()

        # Re-collect
        setup(agents=["claude"], source_home=str(home), dest_dir=dest)
        second_manifest = (dest / "manifest.json").read_text()

        # Both should be valid JSON
        json.loads(first_manifest)
        json.loads(second_manifest)

    def test_setup_credential_content_correct(self, tmp_path: Path):
        """The collected credentials.json content matches the source."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"

        setup(agents=["claude"], source_home=str(home), dest_dir=dest)

        collected_creds = json.loads((dest / "claude" / "credentials.json").read_text())
        assert collected_creds["claudeAiOauth"]["accessToken"] == "tok123"

    def test_setup_config_is_cleaned(self, tmp_path: Path):
        """The collected config.json strips extra projects."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"

        setup(agents=["claude"], source_home=str(home), dest_dir=dest)

        config = json.loads((dest / "claude" / "config.json").read_text())
        # Should only have the container home project, not the original
        assert "/some/path" not in config.get("projects", {})

    def test_setup_unknown_agent_raises(self, tmp_path: Path):
        """Collecting with an unknown agent name raises ValueError."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"

        with pytest.raises(ValueError, match="Unknown agent"):
            setup(agents=["nonexistent"], source_home=str(home), dest_dir=dest)

    def test_setup_missing_credentials_exits(self, tmp_path: Path):
        """Collecting from a home without credentials raises SystemExit."""
        empty_home = tmp_path / "empty_home"
        empty_home.mkdir()
        dest = tmp_path / "my_auth"

        with pytest.raises(SystemExit):
            setup(agents=["claude"], source_home=str(empty_home), dest_dir=dest)


# ---------------------------------------------------------------------------
# get_status()
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_get_status_returns_file_statuses(self, tmp_path: Path):
        """get_status() returns AuthFileStatus for each collected file."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"
        setup(agents=["claude"], source_home=str(home), dest_dir=dest)

        statuses = get_status(auth_dir=dest)

        assert len(statuses) >= 2  # credentials.json + config.json
        assert all(isinstance(s, AuthFileStatus) for s in statuses)
        assert all(s.agent == "claude" for s in statuses)

    def test_get_status_file_types(self, tmp_path: Path):
        """get_status() returns correct types for credential and config files."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"
        setup(agents=["claude"], source_home=str(home), dest_dir=dest)

        statuses = get_status(auth_dir=dest)
        types = {s.file: s.type for s in statuses}

        assert types[".claude/.credentials.json"] == "credential"
        assert types[".claude.json"] == "config"

    def test_get_status_exists_field(self, tmp_path: Path):
        """All collected files should have exists=True."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"
        setup(agents=["claude"], source_home=str(home), dest_dir=dest)

        statuses = get_status(auth_dir=dest)
        assert all(s.exists for s in statuses)

    def test_get_status_no_manifest_raises(self, tmp_path: Path):
        """get_status() raises FileNotFoundError if no manifest."""
        with pytest.raises(FileNotFoundError):
            get_status(auth_dir=tmp_path / "nonexistent")

    def test_get_status_filter_by_agent(self, tmp_path: Path):
        """get_status() filters by agent name when specified."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"
        setup(agents=["claude"], source_home=str(home), dest_dir=dest)

        # Asking for a non-existent agent should return empty
        statuses = get_status(agents=["codex"], auth_dir=dest)
        assert len(statuses) == 0

    def test_get_status_token_expiry(self, tmp_path: Path):
        """get_status() reports token expiry for credential files."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"
        setup(agents=["claude"], source_home=str(home), dest_dir=dest)

        statuses = get_status(auth_dir=dest)
        cred_status = next(s for s in statuses if s.type == "credential")
        # Our fake token has a far-future expiry
        assert cred_status.token_expiry is not None
        assert "valid" in cred_status.token_expiry


# ---------------------------------------------------------------------------
# get_docker_flags()
# ---------------------------------------------------------------------------


class TestGetDockerFlags:
    def test_returns_volume_flag(self, tmp_path: Path):
        """get_docker_flags() returns a proper -v flag string."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"
        setup(agents=["claude"], source_home=str(home), dest_dir=dest)

        flags = get_docker_flags(auth_dir=dest)

        assert flags.startswith("-v ")
        assert str(dest) in flags
        assert ":/tmp/caw_auth:rw" in flags

    def test_no_manifest_raises(self, tmp_path: Path):
        """get_docker_flags() raises FileNotFoundError if no manifest."""
        with pytest.raises(FileNotFoundError):
            get_docker_flags(auth_dir=tmp_path / "nonexistent")

    def test_default_mount_point(self, tmp_path: Path):
        """The default mount point is /tmp/caw_auth."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"
        setup(agents=["claude"], source_home=str(home), dest_dir=dest)

        flags = get_docker_flags(auth_dir=dest)
        assert "/tmp/caw_auth" in flags

    def test_flag_is_usable_in_docker_command(self, tmp_path: Path):
        """The returned flag can be split and used in a docker command."""
        home = _make_fake_home(tmp_path)
        dest = tmp_path / "my_auth"
        setup(agents=["claude"], source_home=str(home), dest_dir=dest)

        flags = get_docker_flags(auth_dir=dest)
        parts = flags.split()
        assert parts[0] == "-v"
        # The volume spec should have exactly 3 colon-separated parts
        vol_parts = parts[1].split(":")
        assert len(vol_parts) == 3
        assert vol_parts[2] == "rw"
