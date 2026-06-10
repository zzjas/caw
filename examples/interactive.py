"""Interactive mode — launch the agent and let the user take over.

Pass ``select_provider=True`` to pick which installed provider to launch from
an arrow-key menu (↑/↓ to move, Enter to choose, q/Esc to cancel) instead of
using the agent's configured provider.
"""

import sys

from caw import Agent


def main():
    # `select_provider` is taken from the first CLI arg: `python interactive.py pick`.
    pick = len(sys.argv) > 1 and sys.argv[1] in ("pick", "select", "--select-provider")

    agent = Agent()

    prompt = (
        "List the directories in the current directory, then wait for me to tell "
        "you which one to count the Python files in."
    )
    result = agent.interactive(prompt, capture_bytes=4096, select_provider=pick)

    print(f"\nExit code: {result.exit_code}")
    if result.session_id:
        print(f"Session ID: {result.session_id}")
    print(f"Captured {len(result.output)} chars of terminal output")


if __name__ == "__main__":
    main()
