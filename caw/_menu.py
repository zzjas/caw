"""Minimal arrow-key selection menu for the terminal.

Used by ``Agent.interactive(select_provider=True)`` to let the user pick a
provider before the agent launches.  Renders to the terminal and reads keys in
raw mode; no third-party dependency.
"""

from __future__ import annotations

import os
import sys


def select_from_menu(title: str, options: list[str], *, default: int = 0) -> int | None:
    """Show an up/down selection menu and return the chosen index.

    Navigation: ``↑``/``↓`` (or ``k``/``j``) move, ``Enter`` selects, ``q`` or
    ``Esc`` cancels.  ``Ctrl-C`` raises ``KeyboardInterrupt``.

    Args:
        title: header line shown above the options.
        options: non-empty list of display strings.
        default: index highlighted initially.

    Returns:
        The selected index, or ``None`` if the user cancelled.

    Raises:
        RuntimeError: if stdin/stdout is not an interactive terminal.
    """
    import select as _select
    import termios
    import tty

    if not options:
        raise ValueError("select_from_menu requires at least one option")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError("select_from_menu requires an interactive terminal (tty).")

    fd = sys.stdin.fileno()
    out = sys.stdout
    n = len(options)
    idx = max(0, min(default, n - 1))

    def render(first: bool) -> None:
        # Re-render in place by moving the cursor back up to the title line.
        # Every line is drawn with a leading CR + clear-to-end so it works in
        # raw mode (where "\n" is a bare line feed, no carriage return).
        if not first:
            out.write(f"\x1b[{n}A")
        out.write(f"\r\x1b[2K{title}")
        for i, opt in enumerate(options):
            pointer = "❯" if i == idx else " "
            line = f" {pointer} {opt}"
            if i == idx:
                line = f"\x1b[36m{line}\x1b[0m"  # cyan highlight
            out.write(f"\n\r\x1b[2K{line}")
        out.flush()

    old_attrs = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        render(True)
        while True:
            ch = os.read(fd, 1)
            if not ch:
                return None
            if ch in (b"\r", b"\n"):
                return idx
            if ch == b"\x03":  # Ctrl-C
                raise KeyboardInterrupt
            if ch in (b"q", b"Q"):
                return None
            if ch == b"\x1b":
                # Could be a bare Esc (cancel) or an arrow escape sequence.
                ready, _, _ = _select.select([fd], [], [], 0.05)
                if not ready:
                    return None
                seq = os.read(fd, 2)
                if seq == b"[A":
                    idx = (idx - 1) % n
                elif seq == b"[B":
                    idx = (idx + 1) % n
                else:
                    continue  # other escape (left/right/home/…): ignore
            elif ch == b"k":
                idx = (idx - 1) % n
            elif ch == b"j":
                idx = (idx + 1) % n
            else:
                continue  # ignore unrelated keys without redrawing
            render(False)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        out.write("\n")
        out.flush()
