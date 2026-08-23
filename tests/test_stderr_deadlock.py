"""A chatty child must not deadlock the turn on its stderr pipe.

stderr was read only after the stdout loop ended, and a pipe holds ~64 KiB: a
CLI that wrote more than that before we finished with stdout blocked in
``write()`` while the provider blocked reading a stdout it could no longer
produce. Neither side ever moved again, at zero CPU.

The live instance: a codex CLI too old for its model logged the server's whole
~140 KB model catalog to stderr before its first API call, and the turn hung
for 52 minutes -- until the pipe was drained from outside, whereupon the real
answer (HTTP 400, "requires a newer version of Codex") arrived in seconds.

Both tests here HANG rather than fail without the drain, so each runs the work
on a thread and fails on the join timeout.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading

import pytest

from caw.providers.claude_code import _STDERR_KEEP, _StderrDrain, _terminate_process_group
from caw.providers.codex import CodexSession

#: Comfortably past a pipe buffer (~64 KiB) so the child would block on write.
_NOISE = 200_000

#: Generous: the work is milliseconds, and this only bounds a regression.
_JOIN_S = 30.0


def _run_bounded(fn):
    """Run *fn* on a thread; fail (not hang) if it does not finish."""
    box: dict[str, object] = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # surfaced below
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(_JOIN_S)
    if t.is_alive():
        pytest.fail(f"deadlocked: did not finish within {_JOIN_S}s -- stderr pipe is not being drained")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("value")


class TestStderrDrain:
    def test_a_child_past_the_pipe_buffer_still_finishes(self):
        """The child writes stderr first, so without the drain it never reaches stdout."""
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"import sys; sys.stderr.write('x' * {_NOISE}); sys.stderr.flush(); print('done')",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        drain = _StderrDrain(proc)
        try:
            out = _run_bounded(lambda: proc.stdout.read())
            assert out.strip() == "done"
            assert proc.wait(timeout=_JOIN_S) == 0
            assert len(drain.text()) > 0
        finally:
            _terminate_process_group(proc, proc.pid)

    def test_the_tail_is_kept_and_the_drop_is_stated(self):
        """Whatever is dropped is dropped from the FRONT, and says so."""
        tail = "TAIL-MARKER"
        proc = subprocess.Popen(
            # Built in the child: a 200 KB literal in argv is itself too long.
            [sys.executable, "-c", f"import sys; sys.stderr.write('H' * {_NOISE} + {tail!r})"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        drain = _StderrDrain(proc)
        try:
            _run_bounded(lambda: proc.wait())
            text = drain.text(timeout=_JOIN_S)
            assert text.endswith(tail)
            assert len(text) < _NOISE
            assert "dropped" in text.splitlines()[0]
        finally:
            _terminate_process_group(proc, proc.pid)

    def test_a_quiet_child_carries_no_drop_notice(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        drain = _StderrDrain(proc)
        try:
            _run_bounded(lambda: proc.wait())
            assert drain.text(timeout=_JOIN_S) == "boom"
        finally:
            _terminate_process_group(proc, proc.pid)

    def test_the_cap_is_a_floor_on_what_is_kept(self):
        """Trimming drops whole chunks, so it may keep more than the cap, never less."""
        assert _STDERR_KEEP >= 64 * 1024


class TestCodexTurnSurvivesAChattyCli:
    """End-to-end through the provider, against the real failure's shape."""

    def _shim(self, tmp_path, script: str) -> None:
        fake = tmp_path / "codex"
        fake.write_text(f"#!{sys.executable}\n{script}")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def test_noisy_stderr_before_the_first_stdout_event(self, tmp_path, monkeypatch):
        # The order is the whole test: all of stderr, THEN the terminal event.
        self._shim(
            tmp_path,
            "import sys, json\n"
            f"sys.stderr.write('E' * {_NOISE}); sys.stderr.flush()\n"
            "print(json.dumps({'type': 'thread.started', 'thread_id': 'th-1'}), flush=True)\n"
            "print(json.dumps({'type': 'turn.completed', "
            "'usage': {'input_tokens': 10, 'output_tokens': 3}}), flush=True)\n",
        )
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

        session = CodexSession(mcp_servers=[])
        turn = _run_bounded(lambda: session.send("hi"))

        assert turn.usage.output_tokens == 3
        assert session.resume_key == "th-1"

    def test_a_noisy_early_exit_still_reports_its_stderr(self, tmp_path, monkeypatch):
        """The error path is why stderr is captured at all -- it must survive the cap."""
        self._shim(
            tmp_path,
            f"import sys\nsys.stderr.write('E' * {_NOISE})\nsys.stderr.write('the real reason')\nsys.exit(3)\n",
        )
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

        session = CodexSession(mcp_servers=[])
        with pytest.raises(RuntimeError, match="the real reason"):
            _run_bounded(lambda: session.send("hi"))
