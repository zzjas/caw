# ToolKit & tools

caw gives you two ways to hand the agent Python tools without writing or running an MCP server
yourself: **stateless functions** and the declarative **`ToolKit`** class. Both are MCP HTTP
servers under the hood, started and stopped automatically with the session.

## Stateless tools

Decorate plain functions with [`@tool`][caw.tool] and pass them via `stateless_tools=`:

```python
from caw import Agent, tool

@tool(description="Add two numbers")
def add(a: int, b: int) -> int:
    return a + b

@tool(description="Multiply two numbers")
def multiply(a: int, b: int) -> int:
    return a * b

agent = Agent(
    system_prompt="You have access to math tools. Use them to answer questions.",
    stateless_tools=[add, multiply],
)
```

Full example — [`examples/tools_simple.py`](https://github.com/zzjas/caw/blob/main/examples/tools_simple.py):

```python
--8<-- "examples/tools_simple.py"
```

## ToolKit: stateful, declarative tool servers

Subclass [`ToolKit`][caw.ToolKit], decorate methods with `@tool`, and caw exposes them as a
single MCP server. Instance state (`self`) persists across tool calls for the whole session:

```python
from caw import Agent, ToolKit, tool

class UserDB(ToolKit, server_name="user_db", display_name="User Database"):
    def __init__(self):
        self.users = ["Alice", "Bob"]

    @tool(description="List all users")
    async def list_users(self) -> str:
        return ", ".join(self.users)

    @tool(description="Add a user")
    async def add_user(self, name: str) -> str:
        self.users.append(name)
        return f"Added {name}"

db = UserDB()
agent = Agent(system_prompt="You have a user database.", tool_servers=[db])
traj = agent.completion("Add Eve to the user database, then list all users")
```

You can pass the `ToolKit` instance directly in `tool_servers=` (caw calls `as_server()` for
you), or call `agent.add_tool_server(db)` later. Methods may be sync or async.

Full example — [`examples/toolkit.py`](https://github.com/zzjas/caw/blob/main/examples/toolkit.py):

```python
--8<-- "examples/toolkit.py"
```

### Thread safety

By default a `ToolKit`'s methods may run concurrently. If your state isn't safe for that,
declare `thread_safe=True` in the subclass options and caw serializes calls with a lock:

```python
class Counter(ToolKit, server_name="counter", thread_safe=True):
    ...
```

## Tool permission groups

Independently of *which* tools you add, you can restrict the agent's **built-in** tools (read,
write, exec, web, …) with [`ToolGroup`][caw.ToolGroup]:

```python
from caw import Agent, ToolGroup

# Read-only: Read/Glob/Grep, but no Bash/Write/Edit/WebSearch.
agent = Agent(tools=ToolGroup.READER)

# Everything except writes:
agent = Agent(tools=ToolGroup.ALL - ToolGroup.WRITER)
```

Groups combine with `|` (union) and `-` (subtract). The default for automated runs is
`ToolGroup.ALL - ToolGroup.INTERACTION`. Full example —
[`examples/tool_groups.py`](https://github.com/zzjas/caw/blob/main/examples/tool_groups.py):

```python
--8<-- "examples/tool_groups.py"
```
