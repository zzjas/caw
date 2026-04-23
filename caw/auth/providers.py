"""Agent auth providers — knows where each agent stores credentials and config."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from .manifest import ManifestFile

console = Console()

# Default container home directory
CONTAINER_HOME = "/home/playground"


@dataclass
class CollectedFile:
    """A file collected from the host, ready to be written to ~/.caw/auth/."""

    manifest_file: ManifestFile
    content: bytes


class AgentAuthProvider(ABC):
    """Base class for agent auth providers."""

    name: str

    @abstractmethod
    def validate(self, src_home: Path) -> list[str]:
        """Return list of missing required file paths (as strings)."""

    @abstractmethod
    def describe(self, src_home: Path) -> str:
        """Return a short account info summary."""

    @abstractmethod
    def collect(self, src_home: Path) -> list[CollectedFile]:
        """Collect cleaned auth/config files. Returns list of CollectedFile."""


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

CLAUDE_JSON_KEEP_KEYS = {
    "oauthAccount",
    "userID",
    "hasCompletedOnboarding",
    "lastOnboardingVersion",
    "numStartups",
    "installMethod",
    "firstStartTime",
    "claudeCodeFirstTokenDate",
    "s1mAccessCache",
    "passesEligibilityCache",
    "groveConfigCache",
    "sonnet45MigrationComplete",
    "opus45MigrationComplete",
    "opusProMigrationComplete",
    "thinkingMigrationComplete",
    "autoUpdates",
}


def _build_clean_claude_json(source: dict) -> dict:
    """Build a minimal .claude.json, keeping only essential keys."""
    clean = {k: source[k] for k in CLAUDE_JSON_KEEP_KEYS if k in source}
    clean["projects"] = {
        CONTAINER_HOME: {
            "allowedTools": [],
            "mcpContextUris": [],
            "mcpServers": {},
            "enabledMcpjsonServers": [],
            "disabledMcpjsonServers": [],
            "hasTrustDialogAccepted": False,
            "projectOnboardingSeenCount": 1,
            "hasClaudeMdExternalIncludesApproved": False,
            "hasClaudeMdExternalIncludesWarningShown": False,
            "exampleFiles": [],
            "lastTotalWebSearchRequests": 0,
        }
    }
    return clean


class ClaudeAuthProvider(AgentAuthProvider):
    name = "claude"

    def validate(self, src_home: Path) -> list[str]:
        missing = []
        if not (src_home / ".claude.json").exists():
            missing.append(str(src_home / ".claude.json"))
        if not (src_home / ".claude" / ".credentials.json").exists():
            missing.append(str(src_home / ".claude" / ".credentials.json"))
        return missing

    def describe(self, src_home: Path) -> str:
        try:
            with open(src_home / ".claude.json") as f:
                cfg = json.load(f)
            account = cfg.get("oauthAccount", {})
            email = account.get("emailAddress", "unknown")
            org = account.get("organizationName", "unknown")

            with open(src_home / ".claude" / ".credentials.json") as f:
                creds = json.load(f)
            expires_at = creds.get("claudeAiOauth", {}).get("expiresAt")
            parts = [f"Account: {email}", f"Org: {org}"]
            if expires_at:
                from datetime import datetime, timezone

                dt = datetime.fromtimestamp(expires_at / 1000, tz=timezone.utc)
                parts.append(f"Token expires: {dt.isoformat()}")
            return ", ".join(parts)
        except Exception:
            return "Could not read account info"

    def collect(self, src_home: Path) -> list[CollectedFile]:
        # credentials.json — credential, symlinked for token refresh write-back
        with open(src_home / ".claude" / ".credentials.json") as f:
            credentials = json.load(f)

        cred_file = CollectedFile(
            manifest_file=ManifestFile(
                src="claude/credentials.json",
                container_target=".claude/.credentials.json",
                host_original=".claude/.credentials.json",
                type="credential",
                strategy="bind",
                mode="0600",
            ),
            content=json.dumps(credentials).encode(),
        )

        # config.json — cleaned .claude.json for containers, copied (not symlinked)
        with open(src_home / ".claude.json") as f:
            local_config = json.load(f)

        clean_config = _build_clean_claude_json(local_config)
        original_keys = len(local_config)
        clean_keys = len(clean_config)
        original_projects = len(local_config.get("projects", {}))
        console.print(f"  [dim]Stripped .claude.json: {original_keys} keys -> {clean_keys} keys[/dim]")
        console.print(f"  [dim]Stripped projects: {original_projects} entries -> 1 entry[/dim]")

        config_file = CollectedFile(
            manifest_file=ManifestFile(
                src="claude/config.json",
                container_target=".claude.json",
                host_original=".claude.json",
                type="config",
                strategy="copy",
                mode="0644",
            ),
            content=json.dumps(clean_config, indent=2).encode(),
        )

        return [cred_file, config_file]


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def _build_clean_codex_config(source_toml: str) -> str:
    """Build a minimal config.toml for codex, stripping local project trust."""
    lines: list[str] = []
    skip_section = False

    for line in source_toml.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            if stripped.startswith("[project_trust."):
                skip_section = True
                continue
            skip_section = False
        if skip_section:
            continue
        lines.append(line)

    lines.append("")
    lines.append(f'[project_trust."{CONTAINER_HOME}"]')
    lines.append('trust_mode = "full"')
    lines.append("")

    return "\n".join(lines)


class CodexAuthProvider(AgentAuthProvider):
    name = "codex"

    def validate(self, src_home: Path) -> list[str]:
        missing = []
        if not (src_home / ".codex" / "auth.json").exists():
            missing.append(str(src_home / ".codex" / "auth.json"))
        return missing

    def describe(self, src_home: Path) -> str:
        try:
            with open(src_home / ".codex" / "auth.json") as f:
                auth_data = json.load(f)
            has_token = bool(auth_data.get("tokens") or auth_data.get("token") or auth_data.get("access_token"))
            has_api_key = bool(auth_data.get("OPENAI_API_KEY"))
            parts = []
            if has_api_key:
                parts.append("API key present")
            if has_token:
                parts.append("OAuth tokens present")
            return ", ".join(parts) if parts else "Auth file found (no recognized keys)"
        except Exception:
            return "Could not read auth info"

    def collect(self, src_home: Path) -> list[CollectedFile]:
        files: list[CollectedFile] = []

        # auth.json — credential, symlinked
        with open(src_home / ".codex" / "auth.json", "rb") as f:
            auth_content = f.read()
        files.append(
            CollectedFile(
                manifest_file=ManifestFile(
                    src="codex/auth.json",
                    container_target=".codex/auth.json",
                    host_original=".codex/auth.json",
                    type="credential",
                    strategy="bind",
                    mode="0600",
                ),
                content=auth_content,
            )
        )

        # config.toml — config, copied (cleaned)
        config_path = src_home / ".codex" / "config.toml"
        if config_path.exists():
            config_text = config_path.read_text()
            clean_config = _build_clean_codex_config(config_text)
            files.append(
                CollectedFile(
                    manifest_file=ManifestFile(
                        src="codex/config.toml",
                        container_target=".codex/config.toml",
                        host_original=".codex/config.toml",
                        type="config",
                        strategy="copy",
                        mode="0644",
                    ),
                    content=clean_config.encode(),
                )
            )
            console.print("  [dim]Stripped config.toml: local project trust -> container trust only[/dim]")

        return files


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, AgentAuthProvider] = {
    p.name: p
    for p in [
        ClaudeAuthProvider(),
        CodexAuthProvider(),
    ]
}
