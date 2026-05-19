# Computer Use

This document covers the built-in Computer Use MCP backend seeding and the normalized desktop-control contract exposed
to the main agent.

## Built-in MCP Backends

This backend can seed two built-in external Computer Use MCP server definitions:

- `computer-use-macos`
- `computer-use-windows`

They are intended to back the main agent's normalized desktop-control contract through the existing MCP discovery path.

Default seeded commands:

- macOS: `uvx macos-mcp`
- Windows: `uvx windows-mcp`

## Configuration

Override the default backend commands with environment variables when needed:

```env
COMPUTER_USE_MACOS_MCP_COMMAND=uvx
COMPUTER_USE_MACOS_MCP_ARGS="macos-mcp"
COMPUTER_USE_WINDOWS_MCP_COMMAND=uvx
COMPUTER_USE_WINDOWS_MCP_ARGS="windows-mcp"
```

## Startup Behavior

- built-in MCP server records are seeded idempotently
- only the backend matching the current host platform is auto-discovered on startup by default
- normalized Computer Use tools are then synced onto the main agent if available

## Operational Requirements

- the configured command must exist on the host
- the command name must be allowlisted by the MCP registry bootstrap
- the external MCP package itself must already be installable on that machine

For the normalized tool contract the main agent sees, use [Tools](./tools.md).
