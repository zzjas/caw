"""Manifest schema for ~/.caw/auth/ — describes all managed auth/config files."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ManifestFile:
    """A single file entry in the manifest."""

    src: str  # relative path within ~/.caw/auth/, e.g. "claude/credentials.json"
    container_target: str  # relative to $HOME in container, e.g. ".claude/.credentials.json"
    host_original: str  # relative to $HOME on host, e.g. ".claude/.credentials.json"
    type: str  # "credential" or "config"
    strategy: str  # "bind" (bind-mounted from host, copy+guard in container) or "copy"
    mode: str  # e.g. "0600"


@dataclass
class AgentManifest:
    """Per-agent manifest."""

    files: list[ManifestFile] = field(default_factory=list)


@dataclass
class Manifest:
    """Top-level manifest for ~/.caw/auth/manifest.json."""

    version: int = 1
    created_at: str = ""
    host_home: str = ""
    container_home: str = "/home/playground"
    mount_point: str = "/tmp/caw_auth"
    agents: dict[str, AgentManifest] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> Manifest:
        agents = {}
        for name, agent_data in data.get("agents", {}).items():
            files = [ManifestFile(**f) for f in agent_data.get("files", [])]
            agents[name] = AgentManifest(files=files)
        return cls(
            version=data.get("version", 1),
            created_at=data.get("created_at", ""),
            host_home=data.get("host_home", ""),
            container_home=data.get("container_home", "/home/playground"),
            mount_point=data.get("mount_point", "/tmp/caw_auth"),
            agents=agents,
        )

    @classmethod
    def load(cls, path: Path) -> Manifest:
        with open(path) as f:
            return cls.from_dict(json.load(f))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(self.to_json())

    @classmethod
    def create(cls, host_home: str) -> Manifest:
        return cls(
            created_at=datetime.now(timezone.utc).isoformat(),
            host_home=host_home,
        )
