# MCP Integration

## Overview

The backend now includes an additive MCP layer under `app/protocols/mcp`. MCP is not the core runtime model. External
MCP tools are discovered and converted into the canonical `ToolDefinition` model with `tool_type = "mcp_tool"`, then
executed through the same native tool path as any other tool.

## Components

- `client.py`: minimal MCP client support, with `stdio` implemented first and HTTP/SSE left as a stub.
- `registry.py`: server registry, command allowlist enforcement, on-demand discovery, and tool invocation.
- `tool_adapter.py`: converts discovered MCP tools into `ToolDefinition`.
- `resource_adapter.py`: normalizes discovered MCP resources.
- `prompt_adapter.py`: normalizes discovered MCP prompts.
- `schemas.py`: internal discovery descriptors for tools, resources, and prompts.

## Domain Model

`MCPServerDefinition` was added to the canonical domain model with:

- `id`
- `name`
- `transport`
- `command`
- `args`
- `url`
- `env_refs`
- `enabled`
- `allowlisted_command`
- `metadata`

`transport = "stdio"` is the active path. HTTP/SSE transport is intentionally stubbed for now.

## Discovery Flow

Discovery is on-demand through the shared API context:

1. Save `MCPServerDefinition`.
2. Call `sync_mcp_catalog()`.
3. The MCP registry validates that the server is enabled and the command is allowlisted.
4. The stdio client issues `initialize`, `tools/list`, `resources/list`, and `prompts/list`.
5. Discovered tools are converted into canonical `ToolDefinition` objects and stored in the normal tool repository.

The catalog route exposes this through `POST /mcp-servers/discover`.

## Execution Flow

Discovered MCP tools are stored with:

- `tool_type = "mcp_tool"`
- `implementation.target = <mcp_server_id>`
- `implementation.config.mcp_tool_name = <remote_tool_name>`

At execution time:

1. Native runtime resolves the tool from the workflow.
2. `ToolExecutor` validates input schema and approval policy.
3. `McpToolExecutor` resolves the server from `MCPClientRegistry`.
4. The MCP client sends `tools/call`.
5. The result is returned through the normal `ToolExecutor` path.

This keeps MCP as an adapter layer, not a new execution model.

## Security Model

- MCP servers are disabled by default.
- MCP server commands must be allowlisted with `MCP_SERVER_COMMAND_ALLOWLIST` or explicitly in the in-memory registry
  configuration.
- No command string is constructed from user input; `command` and `args` come from saved server definitions.
- Discovered MCP tools are constrained with `security.allowlisted_mcp_servers`.
- High-risk MCP tools are inferred from MCP annotations and metadata, and they default to `requires_approval = true`.
- MCP tool calls are logged as normal `ExecutionEvent` records.
- When `redaction_enabled` is set, event payloads are redacted using configured rules before logging.
