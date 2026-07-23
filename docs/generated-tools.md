# Generated Tools

Generated tools are coder-agent-authored Python tool packages that live under
`generated_tools/` instead of `app/tools/implementations/`. The goal is to let the coder agent
build shared tools on demand without mixing experimental or tenant-specific tool code into the core
application implementation tree.

## Lifecycle

There are two separate layers:

1. package scaffold
2. published tool definition

A package scaffold creates files under `generated_tools/<package_id>/` and makes the Python module
discoverable by the backend allowlist. A published tool definition is the record that workflows,
agents, personas, and tool discovery surfaces can actually grant and execute.

Scaffolding a package alone does not make the tool available to agents.

## Package Contract

Each generated package should include:

- `manifest.yaml`
- `tools.py`
- `README.md`
- optional `requirements.txt`

The manifest follows the same broad contract as `integrations/**/manifest.yaml`, but the module
root must start with `generated_tools.`.

Example:

```yaml
id: portal_audit
name: Portal Audit
version: 0.1.0
enabled: true
module_root: generated_tools.portal_audit
tool_modules:
  - generated_tools.portal_audit.tools
requirements_file: requirements.txt
metadata:
  owner: coder-agent
  visibility: shared
  description: Shared portal workflow audit helpers.
```

## Workspace Tools

The backend exposes a generated-tool workspace tool family:

- `agency.tool.workspace.list`
- `agency.tool.workspace.scaffold`
- `agency.tool.workspace.publish`

The portal now exposes the same lifecycle through `/tools/generated` so an operator can scaffold a
shared package or publish a generated callable without dropping to a lower-level tool execution
surface.

### `agency.tool.workspace.list`

Lists generated packages and the published ToolDefinitions already registered against each package.
The response now includes package-level status hints such as:

- `package_state`
- `published_tool_count`
- `readme_path`
- `requirements_path`
- `has_readme`
- `has_requirements`

### `agency.tool.workspace.scaffold`

Creates or refreshes a package folder with:

- `__init__.py`
- `manifest.yaml`
- `README.md`
- `requirements.txt`
- `tools.py`

The scaffold keeps the initial callable small and deterministic so coder-agent edits stay local and
reviewable.

### `agency.tool.workspace.publish`

Validates that the callable exists in the generated package module, then writes or updates a shared
`ToolDefinition` in the tool repository. That published definition is what other agents and personas
can grant and invoke.

Published generated tools are tagged with:

- `generated_tool`
- `generated_package:<package_id>`

They also carry `framework_hints.metadata.generated_tool` so the workspace listing can map published
tools back to the package they came from.

## Portal API Surface

The generated-tools workspace page uses these backend routes:

- `GET /tools/generated/packages`
- `GET /tools/generated/packages/{package_id}`
- `POST /tools/generated/packages/scaffold`
- `POST /tools/generated/packages/publish`

The write endpoints intentionally stay close to the service boundary:

- scaffold creates or refreshes the package files under `generated_tools/<package_id>/`
- publish validates that the generated Python callable exists before saving the shared
  `ToolDefinition`

This keeps package authorship inside the generated workspace while still using the normal tool
repository as the canonical source of agent-usable published tools.

## Discovery Surface

Published generated tools should appear in the same discovery surfaces as other registered tools.

Current behavior:

- runtime execution already resolves granted tools from the tool repository
- `agency.tool.list` and `agency.tool.get` now include published generated tools when running with
  API/runtime context
- builtin-only CLI discovery remains a lower-level catalog view and should not be treated as the
  authoritative list of published shared tools

## Safety Model

Generated tools are published with conservative defaults:

- no shell
- no browser
- no filesystem
- no network
- no dangerous flag
- module/function allowlists pinned to the generated package callable

The publish step may override selected security fields, but the generated workspace service always
re-pins the module and callable allowlists to the declared package entrypoint.
