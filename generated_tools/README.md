# Generated Tools

This folder is reserved for coder-agent-authored tools that should remain
separate from core app implementations.

Each tool package should live under its own subfolder and include:

- `manifest.yaml`
- `tools.py`
- `README.md`
- optional `requirements.txt`

Manifest shape matches the integration discovery contract used under
`integrations/`, but the module root should begin with `generated_tools.`.

Example:

```yaml
id: portal-audit
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
```

Once a package exists here, the Python tool registry allowlist will discover its
declared modules automatically.

Important distinction:

- files under `generated_tools/` only make the Python module importable
- agents and personas can use the tool only after a `ToolDefinition` is published into
  the tool repository, for example through `agency.tool.workspace.publish`

Use the workspace tooling to manage the lifecycle:

- `agency.tool.workspace.list`
- `agency.tool.workspace.scaffold`
- `agency.tool.workspace.publish`

The Agency portal mirrors this lifecycle at `/tools/generated`, including scaffold and publish
actions for operators who need to inspect or register coder-agent-authored packages without editing
registry records by hand.
