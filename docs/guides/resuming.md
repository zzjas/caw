# Resuming sessions

caw can resume a conversation later — in the same process, after a restart, or in a completely
different process. Grab a `resume_handle` (a string), store it anywhere (a database, a file, a
queue), and resume from it.

```python
# Process 1: start, communicate, persist the handle.
agent = Agent(provider="claude_code")
session = agent.start_session()
session.send("My deploy target is staging-eu. Remember that.")
handle = session.resume_handle          # store this string
session.end()

# Process 2 (later, after a restart): resume by handle.
agent = Agent(provider="claude_code")
session = agent.resume_session(handle)
print(session.send("Where am I deploying?").result)   # -> "staging-eu"
session.end()
```

## The handle is self-contained

The handle is a JSON string carrying the backend's own resume key, so resuming works even with
**no `data_dir`** — the underlying CLI still has the conversation:

```json
{"version": 1, "provider": "claude_code", "session_id": "bd260210-…", "resume_key": "bd260210-…"}
```

`resume_key` is Claude's session id, Codex's `thread_id`, or opencode's session id — for
codex/opencode it differs from `session_id`. Resuming works across all three providers.

!!! warning "Treat the handle like a secret"
    The handle grants resume access to the conversation — it is not an opaque random id. Store
    it with the same care as a credential.

!!! note "Send before reading the handle"
    The backend assigns its resume key on the first exchange, so send at least one message
    before reading `session.resume_handle`. Reading it earlier raises.

## `data_dir` is optional and additive

Whether you resume with or without the original `data_dir` changes only how much *caw-side*
history you get back — the backend conversation resumes either way:

| | without `data_dir` | with the original `data_dir` |
|---|---|---|
| backend conversation | resumed | resumed |
| caw trajectory | starts empty | full history restored |
| new turns | not persisted | appended to the original session dir |

A bare session id is also accepted in place of a full handle, but only when `data_dir` is set
(the resume key is then read from disk).

## Full example

[`examples/resume.py`](https://github.com/zzjas/caw/blob/main/examples/resume.py) shows both
the with- and without-`data_dir` paths:

```python
--8<-- "examples/resume.py"
```
