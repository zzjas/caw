"""Resuming a session across processes.

Start a session, store its ``resume_handle`` (a string), then resume the
conversation later with a brand-new Agent — as if a new process picked it up.

The handle is self-contained, so resume works even without a ``data_dir``
(the backend CLI still has the conversation). With a ``data_dir`` you also get
the full trajectory restored and new turns appended. This example shows both.
"""

import os

os.environ["CAW_LOG"] = "full"

from caw import Agent

DATA_DIR = "caw_data"


def main():
    # --- "Process 1": start a session and grab a handle to store somewhere. ---
    agent = Agent(data_dir=DATA_DIR)
    session = agent.start_session()
    session.send("My favorite number is 42. Acknowledge it.")
    handle = session.resume_handle
    session.end()

    print(f"\nStored resume_handle: {handle}\n")
    # In a real app you'd persist `handle` (DB, file, queue) and exit here.

    # --- "Process 2a": a fresh Agent WITH the same data_dir restores history. ---
    agent2 = Agent(data_dir=DATA_DIR)
    resumed = agent2.resume_session(handle)
    turn = resumed.send("What is my favorite number?")
    resumed.end()
    print(f"\n[with data_dir]    recalled: {turn.result!r}")
    print(f"[with data_dir]    turns in trajectory: {resumed.trajectory.num_turns}")

    # --- "Process 2b": a fresh Agent with NO data_dir still resumes the chat. ---
    agent3 = Agent(data_dir=None)
    resumed2 = agent3.resume_session(handle)
    turn2 = resumed2.send("And what is my favorite number again?")
    resumed2.end()
    print(f"\n[without data_dir] recalled: {turn2.result!r}")
    # Trajectory starts empty here, so only this turn is recorded.
    print(f"[without data_dir] turns in trajectory: {resumed2.trajectory.num_turns}")


if __name__ == "__main__":
    main()
