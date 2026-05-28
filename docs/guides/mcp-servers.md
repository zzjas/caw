# MCP servers

[MCP](https://modelcontextprotocol.io/) servers let the agent call external tools. caw
supports both stdio and HTTP transports via the [`MCPServer`][caw.MCPServer] config.

## Attaching an MCP server

```python
from caw import Agent, MCPServer

agent = Agent()
agent.add_mcp_server(MCPServer(
    name="my_db",
    command="python",
    args=["-m", "my_mcp_server"],
))

traj = agent.completion("Use the my_db tools to count rows in the users table.")
```

## stdio vs. HTTP transport

`MCPServer` picks the transport from which fields you set:

- **stdio** — set `command`, `args`, and optionally `env`. caw launches the server as a
  subprocess and speaks MCP over its stdin/stdout.
- **HTTP** — set `url`. `command`/`args`/`env` are ignored.

```python
# stdio
MCPServer(name="fs", command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "/data"])

# HTTP
MCPServer(name="remote", url="http://localhost:9000/mcp")
```

You can register multiple servers; configured servers are recorded on the trajectory
(`trajectory.mcp_servers`).

## Defining tools in Python instead

If you'd rather write tools as Python functions or classes than run a separate MCP server, caw
spins one up for you:

- **[`ToolKit`](toolkit.md)** — declarative tool classes with `@tool` methods, served over a
  managed HTTP MCP server.
- **Stateless functions** — pass plain `@tool`-decorated functions via `stateless_tools=`.

Both are MCP under the hood; the difference is you don't author or run the server yourself.

## API

See [`MCPServer`][caw.MCPServer] and the lower-level
[Tools & MCP reference](../reference/api/toolkit-mcp.md) for `MCPServerHandle`,
`create_mcp_http_server_bundle`, and the `mcp_tool` decorator.
