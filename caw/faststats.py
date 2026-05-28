"""Fast extraction of frequently-needed statistics from trajectory files.

A full ``Trajectory.from_dict(json.loads(...))`` round-trip is slow on large
trajectories because the ``turns`` array can hold many MB of tool I/O that
the caller does not need. Most consumers (cost dashboards, spend limiters,
list views) only want a handful of header / footer fields:

    cost_usd, model, created_at, completed_at, duration_ms, token totals.

``FastStats`` extracts these by reading only the head and tail of the file
(~8 KB total) and parsing the predictable indent=2 layout that
`caw.storage.SessionStore` writes. The fast path is roughly 3x
quicker than ``json.loads`` on a directory of small trajectories and 25x+
faster on multi-MB files. When the fast path fails (non-CAW layout, hand
edited file, etc.) it falls back to a full JSON parse.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from caw.models import Trajectory

__all__ = ["FastStats"]

# Bytes read from the head and tail of each file. The CAW writer puts every
# header field (agent, model, session_id, created_at, completed_at,
# usage_limited) in the first ~300 bytes and the trailing usage / total_usage
# / duration_ms / metadata block in the last few hundred bytes, so 4 KB on
# each side is plenty of headroom even for files with long ``system_prompt``
# values stretching the header.
_HEAD_BYTES = 4096
_TAIL_BYTES = 4096


def _str_field(blob: str, key: str) -> str:
    """Return the first JSON string value for ``"key": "..."`` in *blob*."""
    m = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:[^"\\]|\\.)*)"', blob)
    if not m:
        return ""
    raw = m.group(1)
    # Decode JSON escapes (e.g. \", \\, \n, \uXXXX) by parsing as a JSON string
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw


def _num_field(blob: str, key: str, *, default: float = 0.0) -> float:
    """Return the first JSON number value for ``"key": <num>`` in *blob*."""
    m = re.search(rf'"{re.escape(key)}"\s*:\s*([0-9eE.+-]+)', blob)
    return float(m.group(1)) if m else default


def _bool_field(blob: str, key: str) -> bool:
    m = re.search(rf'"{re.escape(key)}"\s*:\s*(true|false)', blob)
    return bool(m and m.group(1) == "true")


@dataclass
class FastStats:
    """Lightweight statistics for a CAW trajectory.

    The class is intentionally narrow: it exposes only the fields that
    consumers ask for repeatedly without paying for a full trajectory parse.
    For everything else (turns, tool calls, content blocks) load the file
    via `caw.agent.Session.load_trajectory` instead.

    All ``cost_usd`` / token values come from the trajectory's
    ``total_usage`` (recursive across subagents) when present, falling back
    to ``usage`` for older trajectories that did not record it separately.
    """

    path: Optional[Path] = None

    # Header fields (from the start of the file).
    agent: str = ""
    model: str = ""
    session_id: str = ""
    created_at: str = ""
    completed_at: str = ""
    usage_limited: bool = False

    # Footer fields (from the tail of the file).
    duration_ms: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict (``path`` is stringified)."""
        d = asdict(self)
        d["path"] = str(self.path) if self.path is not None else None
        return d

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_trajectory(cls, trajectory: Trajectory, *, path: str | Path | None = None) -> FastStats:
        """Build `FastStats` from an in-memory `Trajectory`."""
        usage = trajectory.total_usage
        return cls(
            path=Path(path) if path is not None else None,
            agent=trajectory.agent,
            model=trajectory.model,
            session_id=trajectory.session_id,
            created_at=trajectory.created_at,
            completed_at=trajectory.completed_at,
            usage_limited=trajectory.usage_limited,
            duration_ms=trajectory.duration_ms,
            cost_usd=usage.cost_usd,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )

    @classmethod
    def from_path(cls, path: str | Path) -> Optional[FastStats]:
        """Read fast stats from *path*.

        Returns ``None`` if the file does not exist, is empty, or is not a
        recognizable trajectory file. Tries the head/tail fast path first
        and falls back to a full JSON parse on failure.
        """
        path = Path(path)
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size == 0:
            return None

        try:
            with open(path, "rb") as f:
                head_len = min(_HEAD_BYTES, size)
                head = f.read(head_len).decode("utf-8", errors="replace")
                if size <= _HEAD_BYTES:
                    tail = head
                elif size <= _HEAD_BYTES + _TAIL_BYTES:
                    tail = head + f.read().decode("utf-8", errors="replace")
                else:
                    f.seek(size - _TAIL_BYTES)
                    tail = f.read(_TAIL_BYTES).decode("utf-8", errors="replace")
        except OSError:
            return None

        stats = cls._fast_extract(head, tail, path)
        if stats is not None:
            return stats

        # Fallback: parse the full document. ``Trajectory.from_dict``
        # tolerates missing keys, so we explicitly require ``model`` to be
        # present and non-empty before treating the file as a trajectory.
        try:
            data = json.loads(path.read_bytes())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not data.get("model"):
            return None
        try:
            traj = Trajectory.from_dict(data)
        except (ValueError, KeyError, TypeError):
            return None
        return cls.from_trajectory(traj, path=path)

    # ------------------------------------------------------------------
    # Directory iteration
    # ------------------------------------------------------------------

    @classmethod
    def iter_directory(
        cls,
        directory: str | Path,
        *,
        patterns: Iterable[str] = ("**/trajectory.json", "**/*.traj.json"),
        skip_parts: Iterable[str] = (),
    ) -> Iterator[FastStats]:
        """Yield `FastStats` for every trajectory file under *directory*.

        ``patterns`` is a list of globs (relative to *directory*) to scan;
        the default catches both the canonical CAW layout
        (``sessions/<id>/trajectory.json``) and the ``.traj.json`` files
        produced by ad-hoc writers. Files whose path contains any directory
        component listed in ``skip_parts`` are excluded. Unreadable or
        malformed files are silently dropped.
        """
        directory = Path(directory)
        if not directory.is_dir():
            return
        skip_set = set(skip_parts)
        seen: set[Path] = set()
        for pattern in patterns:
            for file in directory.glob(pattern):
                if file in seen or not file.is_file():
                    continue
                seen.add(file)
                if skip_set:
                    try:
                        rel_parts = file.relative_to(directory).parts
                    except ValueError:
                        rel_parts = file.parts
                    if any(part in skip_set for part in rel_parts):
                        continue
                stats = cls.from_path(file)
                if stats is not None:
                    yield stats

    @classmethod
    def directory_total_cost(
        cls,
        directory: str | Path,
        **kwargs: Any,
    ) -> float:
        """Sum ``cost_usd`` across every trajectory under *directory*.

        Extra keyword arguments are forwarded to `iter_directory`.
        """
        return sum(s.cost_usd for s in cls.iter_directory(directory, **kwargs))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @classmethod
    def _fast_extract(cls, head: str, tail: str, path: Path) -> Optional[FastStats]:
        """Pull fields from the raw head/tail text. Returns ``None`` on miss."""
        # ``model`` is a required CAW field. Its absence in the head means
        # this isn't a CAW trajectory and the caller should fall back.
        model = _str_field(head, "model")
        if not model:
            return None

        agent = _str_field(head, "agent")
        session_id = _str_field(head, "session_id")
        created_at = _str_field(head, "created_at")
        completed_at = _str_field(head, "completed_at")
        usage_limited = _bool_field(head, "usage_limited")

        # The trailing top-level usage block. With indent=2 the canonical
        # writer always emits ``\n  "total_usage": {`` (or ``"usage": {``)
        # at column 2 — nested usage blocks inside ``turns`` use indent 6+,
        # so anchoring on the 2-space form is unambiguous.
        anchor = tail.rfind('\n  "total_usage": {')
        if anchor == -1:
            anchor = tail.rfind('\n  "usage": {')
        if anchor == -1:
            return None

        trailing = tail[anchor:]
        # Find the first matching closing brace. ``UsageStats`` has no
        # nested objects so a simple ``find`` is correct.
        end = trailing.find("}")
        if end == -1:
            return None
        usage_blob = trailing[:end]

        cost_usd = _num_field(usage_blob, "cost_usd")
        input_tokens = int(_num_field(usage_blob, "input_tokens"))
        output_tokens = int(_num_field(usage_blob, "output_tokens"))
        cache_read_tokens = int(_num_field(usage_blob, "cache_read_tokens"))
        cache_write_tokens = int(_num_field(usage_blob, "cache_write_tokens"))

        # ``duration_ms`` lives at the very end of the file, after the usage
        # blocks. Search the slice past the usage block we just consumed to
        # avoid matching any per-turn ``duration_ms`` that might still be in
        # the tail buffer.
        post_usage = trailing[end:]
        m = re.search(r'\n  "duration_ms"\s*:\s*([0-9]+)', post_usage)
        duration_ms = int(m.group(1)) if m else 0

        return cls(
            path=path,
            agent=agent,
            model=model,
            session_id=session_id,
            created_at=created_at,
            completed_at=completed_at,
            usage_limited=usage_limited,
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
