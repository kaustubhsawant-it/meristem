# Connecting MCP clients to Meristem

The `meristem` MCP server is standard [Model Context Protocol](https://modelcontextprotocol.io),
so any MCP-capable agent can call its tools (`query_facts`, `assert_fact`,
`supersede`, `graph_neighbors`, `embed_status`, `ping`).

Prereq: `pip install 'meristem[mcp]'` (or `uv tool install 'meristem[mcp]'`) so `meristem mcp` runs.

All the desktop/IDE clients below use the same `mcpServers` shape from
[`stdio.mcp.json`](stdio.mcp.json) — set `cwd` to the repo whose `.meristem/` you
want the agent to read. Only the file location differs:

| Client | Config file |
|---|---|
| Claude Desktop | `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) |
| Claude Code | `.mcp.json` in the project root (or `claude mcp add`) |
| Cursor | `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global) |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| Cline (VS Code) | Cline panel → MCP Servers → Configure, or the extension's `cline_mcp_settings.json` |
| Continue | `~/.continue/config.json` under `mcpServers` |

Paste the `mcpServers` block, set `command`/`args`/`cwd`, restart the client.

## Remote / web agents (http or sse)

For agents that connect over the network rather than spawning a subprocess,
serve over a port:

```bash
meristem mcp --transport http --host 127.0.0.1 --port 8765   # or --transport sse
```

Then point the client's MCP server URL at `http://127.0.0.1:8765`.

## What you get vs. Claude Code

Any MCP client gets the **tools** — it queries the graph and writes facts back
by calling them explicitly. The **ambient** layer (auto SessionStart digest,
per-turn context injection, PostToolUse liveness watcher, PreCompact handoff,
Memory Pulse statusline, `/meristem:*` skills) is delivered through Claude Code
hooks and is Claude-Code-specific for now.
