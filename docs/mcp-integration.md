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

For stdio servers, `env_refs` are resolved before the MCP subprocess starts. Use `CredentialReference.key` as the
environment variable name passed to the subprocess and `CredentialReference.ref` as the secret reference, for example
`{"key": "FIRECRAWL_API_KEY", "ref": "env://FIRECRAWL_API_KEY"}`. Secret values are not persisted in the MCP server
record.

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
- MCP server executables must be allowlisted with `MCP_SERVER_ALLOWED_COMMANDS`; the default permits only `npx` and
  `uvx` for the built-in package-based adapters.
- In production, `npx` packages must use an exact numeric version such as `package@1.2.3`, and `uvx` packages must use
  an exact equality pin such as `package==1.2.3`. Tags, ranges, unversioned names, and package-selection flags are
  rejected before an MCP subprocess is created. Configure exact reviewed versions for enabled built-in servers;
  unversioned compatibility defaults fail closed during discovery.
- `MCP_SERVER_ALLOWED_ENV_VARS` limits both the source environment variables and child-process target keys. For
  `env://` references the source and target must match, preventing an allowed child key from laundering another
  backend secret.
- Stdio MCP subprocesses receive a normalized PATH from `MCP_SERVER_EXTRA_PATHS` plus common system locations so `npx`,
  `uvx`, `node`, and Docker-backed servers are discoverable without hardcoding machine-specific absolute paths.
- No command string is constructed from user input; `command` and `args` come from saved server definitions.
- Discovered MCP tools are constrained with `security.allowlisted_mcp_servers`.
- High-risk MCP tools are inferred from MCP annotations and metadata, and they default to `requires_approval = true`.
- MCP tool calls are logged as normal `ExecutionEvent` records.
- When `redaction_enabled` is set, event payloads are redacted using configured rules before logging.

## Built-In Research MCP

The backend seeds a built-in Firecrawl MCP server record with id `research-firecrawl`. It uses stdio by default:

```json
{
  "command": "npx",
  "args": ["-y", "firecrawl-mcp@3.22.3"],
  "env_refs": [{"key": "FIRECRAWL_API_KEY", "ref": "env://FIRECRAWL_API_KEY"}]
}
```

Set `FIRECRAWL_API_KEY` to enable it automatically at startup. The startup path discovers it when enabled, which turns
Firecrawl MCP capabilities such as web search, scraping, crawling, and extraction into normal Agency tools.

Configuration knobs:

- `FIRECRAWL_MCP_ENABLED`
- `FIRECRAWL_MCP_COMMAND`
- `FIRECRAWL_MCP_ARGS`
- `FIRECRAWL_MCP_API_KEY_REF`

## Built-In Developer Docs MCP

The backend seeds a built-in Context7 MCP server record with id `docs-context7`. It uses stdio by default:

```json
{
  "command": "npx",
  "args": ["-y", "@upstash/context7-mcp@3.2.3"],
  "env_refs": [{"key": "CONTEXT7_API_KEY", "ref": "env://CONTEXT7_API_KEY"}]
}
```

Set `CONTEXT7_API_KEY` to enable it automatically at startup. You may also set `CONTEXT7_MCP_ENABLED=true` to run the
public Context7 MCP server without a key at lower public rate limits. Context7 exposes documentation tools such as
library-id resolution and documentation querying for current library/API references.

Configuration knobs:

- `CONTEXT7_API_KEY`
- `CONTEXT7_MCP_ENABLED`
- `CONTEXT7_MCP_COMMAND`
- `CONTEXT7_MCP_ARGS`
- `CONTEXT7_MCP_API_KEY_REF`
