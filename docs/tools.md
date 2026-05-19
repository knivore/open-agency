# Tools

## Overview

Tools are resolved through two boundaries:

- app-owned built-ins under `app/tools/implementations/**`
- user-extensible integrations under `integrations/**`

The canonical layers are:

- `app/domain/tools.py`
- `app/tools/definitions.py`
- `app/tools/registry.py`
- `app/tools/executors/`
- `app/tools/implementations/`
- `integrations/`

## Naming Contract

Tool identity is intentionally split so agents and humans do not compete over the same field:

- `id`: stable persistence, routing, and registry identity, for example `agency.memory.delete`
- `name`: callable-safe tool name exposed to models and runtimes, for example `delete_memory`
- `display_name`: human-facing label for frontend and logs, for example `Delete Memory`
- `implementation.callable_name` or executor metadata: implementation target or upstream remote callable, not a UI label

Agents, model tool payloads, CLI discovery, workflow execution, and runtime persistence should use `name`. Frontend
surfaces should render `display_name` through the frontend helper and only fall back to formatting `name` when older
records do not include a display label.

MCP tools follow the same Agency-facing contract. The normalized Agency `name` is callable-safe, the generated
`display_name` is human-readable, and upstream remote names stay in MCP implementation metadata such as
`mcp_tool_name`.

## Tool Types

The current architecture supports these categories:

- Python function tools
- HTTP or API request tools
- shell or system tools
- SQL or data-query tools
- browser and document tools
- MCP-backed tools
- workflow or orchestration tools
- approval-gated tools

Each tool should resolve through `implementation.module` and `implementation.function` metadata rather than ad hoc
import logic.

## Security Model

Tool definitions can carry security metadata such as:

- `requires_approval`
- `sandbox`
- `allowed_paths`
- `allowed_domains`
- `read_only`
- `dangerous`

This metadata is used to:

- block unsafe execution by default
- require explicit approval for dangerous actions
- scope file-system and network access
- separate safe read-only utilities from state-changing tools

## Approval Model

Approval-gated tools are expected to:

1. declare their approval requirement in tool metadata
2. trigger an approval request before execution
3. pause the execution until approval or rejection
4. emit canonical execution events and invocation records for the decision

The approval model is enforced in the runtime layer, not inside arbitrary tool code.

## Executors

Executors live under `app/tools/executors/` and provide typed execution boundaries such as:

- `python_function.py`
- `http_request.py`
- `sql_query.py`
- `shell_command.py`
- `mcp_tool.py`
- `workflow_tool.py`
- `human_approval.py`

They are responsible for validation, dispatch, and consistent invocation recording.

## Command-Oriented Tooling

The backend exposes a canonical command tool for agents that benefit from Unix-style composition:

- tool id: `agency.command.run`
- callable name: `run_command`
- display name: `Run Command`
- input: `command`, optional `mode`, optional `cwd`, optional `timeout_seconds`
- supported modes: `auto`, `bash`, `sh`, `zsh`, `powershell`, `pwsh`, `cmd`

This tool is intentionally a shell boundary, not a replacement for all typed tools. Prefer it for workflows where CLI
composition is the clearest interface, such as `grep`, `sort`, `head`, `tail`, `curl`, scripts, and shell chains using
`|`, `&&`, `||`, or `;`. Prefer typed tools for high-security operations, strongly structured APIs, database queries,
and cases where schema validation is more important than command composition.

Agent-facing command results include:

- raw `stdout` and `stderr`
- `exit_code` and `duration_ms`
- `output_text` with a stable `[exit:N | duration]` footer
- stderr attached in the presentation output when present
- binary-output guards that avoid feeding non-text bytes to the model
- truncation with an overflow file path and follow-up exploration hints for large output

Long-running workflows can request a bounded `timeout_seconds` override. This is the preferred path for CLI-first
developer automation such as local Codex runs, builds, and test suites when a typed tool would only duplicate shell
behavior.

Shell tools remain approval-gated and sandbox-marked by definition. Do not expose unrestricted shell execution to
untrusted users or external channels without an execution sandbox and human approval policy.

Command guardrails currently block high-risk patterns before execution, including `sudo`, user switching, `git push`,
SSH/SCP, `curl | bash` or `wget | bash`, credential reads such as `cat ~/.ssh/*`, `cat ~/.aws/*`, and `cat .env`, plus
broad recursive deletion or permission changes against `/` or `$HOME`. These blocks are separate from approval: an
approved command can still be rejected by the executor if it matches a blocked pattern.

## MCP Tool Support

MCP-specific behavior should stay inside:

- `app/protocols/mcp`
- `app/tools/executors/mcp_tool.py`

Tool definitions may reference MCP-related metadata, but the protocol transport and invocation handling should remain
isolated from general tool registration.

## Computer Use Contract

Computer Use is exposed through MCP-backed tools, but the main agent should depend on the Agency-normalized contract
rather than raw upstream macOS or Windows tool names.

The normalization layers live in:

- `app/protocols/mcp/tool_adapter.py`
- `app/protocols/mcp/computer_use_adapter.py`
- `app/tools/executors/mcp_tool.py`

### Canonical Tool Names

The current cross-platform Computer Use vocabulary is:

- `snapshot`
- `screenshot`
- `click`
- `type`
- `scroll`
- `move`
- `press_key`
- `wait`
- `app`
- `shell`
- `scrape`

Platform-specific extras may also be normalized when discovered:

- `multi_select`
- `multi_edit`
- `clipboard`
- `process`
- `notification`
- `registry`

### Canonical Input Shapes

The main agent should target these canonical arguments:

- `click`
    - `x: number`
    - `y: number`
    - `button: string | null`
    - `double_click: boolean | null`
- `type`
    - `text: string`
    - `x: number | null`
    - `y: number | null`
    - `clear: boolean | null`
- `scroll`
    - `direction: string | null`
    - `amount: number | null`
    - `x: number | null`
    - `y: number | null`
    - `dx: number | null`
    - `dy: number | null`
- `move`
    - `x: number | null`
    - `y: number | null`
    - `drag: boolean | null`
    - `from_x: number | null`
    - `from_y: number | null`
    - `to_x: number | null`
    - `to_y: number | null`
    - `duration_ms: number | null`
- `press_key`
    - `keys: string`
- `wait`
    - `seconds: number`
- `snapshot`
    - `display: integer[] | null`
    - `use_vision: boolean | null`
    - `use_dom: boolean | null`
    - `annotate: boolean | null`
- `screenshot`
    - `display: integer[] | null`
- `app`
    - `action: string | null`
    - `name: string | null`
    - `bundle_id: string | null`
    - `window_title: string | null`
    - `x: number | null`
    - `y: number | null`
    - `width: number | null`
    - `height: number | null`
- `shell`
    - `command: string`
    - `mode: string | null`
- `scrape`
    - `url: string | null`
    - `use_dom: boolean | null`
    - `selector: string | null`

### Canonical Output Shape

Computer Use MCP execution is normalized to this wrapper:

```json
{
  "status": "ok",
  "tool_family": "computer_use",
  "tool": "snapshot",
  "platform": "macos",
  "remote_tool_name": "Snapshot",
  "request": {},
  "remote_request": {},
  "data": {},
  "raw_result": {}
}
```

Expected fields:

- `status`
    - best-effort normalized tool status such as `ok` or `error`
- `tool_family`
    - always `computer_use`
- `tool`
    - the Agency canonical tool name
- `platform`
    - backend platform such as `macos` or `windows`
- `remote_tool_name`
    - the original upstream MCP tool name
- `request`
    - canonical Agency arguments supplied by the caller
- `remote_request`
    - translated arguments actually sent to the upstream MCP tool
- `data`
    - best-effort canonical result payload
- `raw_result`
    - unmodified upstream MCP response for debugging and compatibility

The `data` block is specialized by tool type. For example:

- `snapshot` or `screenshot`
    - `image`
    - `elements`
    - `windows`
    - `displays`
    - `cursor`
    - `text`
- `shell`
    - `stdout`
    - `stderr`
    - `exit_code`
- `scrape`
    - `url`
    - `title`
    - `text`
    - `markdown`
    - `html`

### Rules

- Main-agent prompts and backend services should depend on canonical Agency tool names and canonical input/output
  shapes.
- Do not hardcode upstream tool names such as `Shortcut` or `Snapshot` outside the normalization layer.
- Preserve `raw_result` for debugging rather than extending business logic around platform-specific payload details.

## Remaining Compatibility Seams

Tool implementations are now split between app-owned built-ins and integration-owned runtime code. The remaining
compatibility boundary is framework-level wrapping inside `app/runtime/adapters/crewai/tools.py`.

New built-in tool development should add implementations under `app/tools/implementations`.

New mutable or user-extensible runtime code should live under `integrations/` with a manifest-driven structure.
