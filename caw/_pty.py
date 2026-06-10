"""Shared pty driver for interactive provider sessions.

Every provider that supports ``start_interactive`` launches a full-screen TUI
binary and bridges it to the user's real terminal: stdin/stdout/stderr are
connected to a pty whose master end is mirrored to the controlling terminal in
raw mode, so the user drives the child's TUI normally while a tail-limited copy
of the child's output is captured.  This module factors that boilerplate out so
each provider only has to build its argv.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from caw.models import InteractiveResult


def drive_interactive_pty(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    capture_bytes: int = 0,
    on_exit: Callable[[], None] | None = None,
) -> InteractiveResult:
    """Run *cmd* attached to a pty, bridged to the user's terminal.

    Args:
        cmd: argv to ``execvp`` in the child.
        env: extra environment variables to set in the child (merged on top of
            the inherited environment; ``None`` means inherit unchanged).
        capture_bytes: maximum bytes of child output to retain (tail).  ``0``
            (default) keeps everything.
        on_exit: optional callback run in the ``finally`` block (e.g. to unlink
            a temporary config file), after terminal state is restored.

    Returns:
        An ``InteractiveResult`` with the child's exit code and captured output.
    """
    import fcntl
    import pty
    import select
    import signal
    import termios
    import tty

    captured = bytearray()
    cap_limit = capture_bytes if capture_bytes > 0 else 0
    master_fd, slave_fd = pty.openpty()

    # Propagate the real terminal size to the pty.
    def _copy_winsize(from_fd: int, to_fd: int) -> None:
        winsize = fcntl.ioctl(from_fd, termios.TIOCGWINSZ, b"\x00" * 8)
        fcntl.ioctl(to_fd, termios.TIOCSWINSZ, winsize)

    _copy_winsize(pty.STDOUT_FILENO, master_fd)

    pid = os.fork()
    if pid == 0:
        # Child: become session leader, set slave as controlling terminal.
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        if env:
            for k, v in env.items():
                os.environ[k] = v
        os.execvp(cmd[0], cmd)

    # Parent.
    os.close(slave_fd)

    # Forward SIGWINCH to the child pty.
    old_sigwinch = signal.getsignal(signal.SIGWINCH)

    def _on_winch(signum: int, frame: object) -> None:
        _copy_winsize(pty.STDOUT_FILENO, master_fd)
        os.kill(pid, signal.SIGWINCH)

    signal.signal(signal.SIGWINCH, _on_winch)

    # Save and set raw mode on the real terminal.
    old_attrs = termios.tcgetattr(pty.STDIN_FILENO)
    tty.setraw(pty.STDIN_FILENO)

    try:
        while True:
            try:
                fds = select.select([pty.STDIN_FILENO, master_fd], [], [], 0.1)[0]
            except select.error:
                continue
            if master_fd in fds:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                captured.extend(data)
                if cap_limit and len(captured) > cap_limit:
                    del captured[: len(captured) - cap_limit]
                os.write(pty.STDOUT_FILENO, data)
            if pty.STDIN_FILENO in fds:
                data = os.read(pty.STDIN_FILENO, 4096)
                if not data:
                    break
                os.write(master_fd, data)

        _, status = os.waitpid(pid, 0)
        exit_code = os.waitstatus_to_exitcode(status)
    except (KeyboardInterrupt, Exception):
        os.kill(pid, signal.SIGTERM)
        os.waitpid(pid, 0)
        raise
    finally:
        # Restore terminal state, signal handler, and clean up.
        termios.tcsetattr(pty.STDIN_FILENO, termios.TCSAFLUSH, old_attrs)
        signal.signal(signal.SIGWINCH, old_sigwinch)
        os.close(master_fd)
        if on_exit is not None:
            on_exit()

    output = captured.decode("utf-8", errors="replace")
    return InteractiveResult(exit_code=exit_code, output=output)
