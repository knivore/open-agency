# Tool Contracts

Tool contracts define the stable boundary between agents and Agency capabilities.

Each hand-authored contract is a JSON document under `app/tools/contracts/schemas/*.contract.json` with:

- `name` and `version` for capability discovery
- `inputs` JSON Schema for request validation
- `outputs` JSON Schema for response validation
- `description` for agent-readable tool selection

Runtime flow:

```text
Agent -> Contract Validator -> Policy Engine -> Tool Runtime -> Structured Response -> Tool Run Store -> Runtime Events
```

Current endpoints:

- `GET /tools/contracts` lists available contracts.
- `GET /tools/contracts/{tool_name}` returns one JSON-LD contract.
- `POST /tools/{tool_name}/run` validates, policy-checks, executes, persists, signs, and returns a structured result.
- `GET /capabilities` exposes contract URLs, run URLs, execution-mode metadata, side-effect hints, policy notes, event-stream URLs, and optional module availability for external agents.
- The `modules` block in `GET /capabilities` advertises optional backend module availability for frontend gating. Setting `SMART_HOME_MODULE_ENABLED=false` or `PHYSICAL_DEVICES_MODULE_ENABLED=false` keeps those surfaces hidden/disabled on compatible frontends.

Current contract-backed tools:

- `sandbox-edit` validates and returns dry-run patch output in `patch` and `filesChanged`.
- Active built-in tools are present in the default contract registry. Fully bridged tools use hand-authored JSON contracts; remaining context-bound built-ins use generated contracts derived from canonical `ToolDefinition` input schemas.
- Generated built-in contracts preserve Agency schema extensions such as `x-agency-filled-by` and
  `x-agency-user-visible`, so callers can distinguish workflow-author defaults from agent-supplied
  runtime inputs without reverse-engineering individual tool behavior.
- Generated built-in contracts are derived from the assembled builtin registry in
  `app/tools/builtins.py`, so contract generation tracks the same tool surface used by CLI discovery,
  runtime inspection, and seed data.
- Browser tools, `agency.human.ask`, and `agency.workflow.run` now have hand-authored contracts with explicit input schemas and wrapped `ToolRunResponse.result` output shapes.
- `agency.tool.list` returns the live built-in tool catalog in `result.items` with `result.count`.
- `agency.command.run` runs one policy-mediated shell command and returns stdout, stderr, exit code, duration, truncation state, and overflow path metadata in `result`.
- `agency.file.write-text` writes or appends text under `TOOL_FILE_WRITE_ALLOWED_DIRS` and returns status/path metadata in `result` plus `filesChanged`.
- `agency.excel.write-text`, `agency.excel.write-json`, and `agency.excel.write-image` modify allowlisted workbooks from allowlisted source files or images, then return normalized status/workbook/source metadata in `result` plus `filesChanged`.
- `agency.document.markdown-to-word` converts markdown to a Word document, uploads it through Agency storage, and returns normalized status/storage URI metadata in `result`.
- `agency.http.request` sends one allowlisted HTTP/HTTPS request and returns status code, parsed response payload, method, and URL metadata in `result`.
- `agency.workflow.list`, `agency.workflow.get`, and `agency.tool.get` expose read-only workflow/tool discovery through signed contract responses. Workflow reads require an API context-backed runtime route.
- `agency.execution.get`, `agency.execution.events`, and `agency.execution.artifacts` expose read-only execution
  inspection for eval agents and contract clients.
- `agency.graph.context` exposes read-only Agency Graph context when `AGENCY_GRAPH_CONTEXT_TOOLS_ENABLED=true`.
  Anchor-based calls use `anchor_type` plus `anchor_id`; query-based calls use `query` with optional scope labels.
  Responses are bounded, signed, provenance-carrying `ToolRunResponse.result` payloads.
- `agency.memory.list`, `agency.memory.remember`, `agency.memory.update`, and `agency.memory.delete` expose durable memory CRUD through signed contract responses and use `MemoryService` for ownership and sensitive-confirmation policy.
- `agency.workflow.propose-create`, `agency.workflow.propose-update`, `agency.tool.propose-create`, and `agency.tool.propose-update` are contracted as approval-mediated proposal tools. Calls with `conversation_id` create approval requests; calls without conversation context return signed `requires_conversation_context` responses.
- `agency.workflow.run` creates and queues unprotected workflow executions through `ExecutionService`, then returns execution id, workflow id, queued status, and the execution payload in `result`.
- Browser tools execute through the existing browser session manager. `agency.browser.open` is URL/host policy checked; click/select/type actions are policy-mediated and warn when the actor is not explicitly approved.
- `agency.human.ask` publishes a prompt to the existing human input channel and waits up to `timeout_seconds` for a reply.
- Protected workflow runs and proposal tools create real conversation approval requests when `conversation_id` is supplied. Without conversation context, direct contract runs return signed `requires_approval_context` or `requires_conversation_context` responses instead of bypassing human approval.

Use `patch` for diff-oriented file changes and `result` for structured non-diff payloads.

To add a contract:

1. Add `app/tools/contracts/schemas/<tool>.contract.json`.
2. Keep input and output schemas as JSON object schemas.
3. Add policy handling in `app/tools/policies` when execution can mutate files, call external systems, or expose secrets.
4. Add runtime handling in `app/tools/runtime/executor.py`.
5. Add tests for load, validation, policy verdicts, runtime response shape, persistence, and events.

Tool runs are persisted to `TOOL_RUN_STORE_PATH`, defaulting to `.data/executions/tool_runs.jsonl`.
File-write and spreadsheet-writer contract runs are constrained by `TOOL_FILE_WRITE_ALLOWED_DIRS`, defaulting to the backend and frontend workspace roots.
Sandbox edit contract runs are constrained by `SANDBOX_EDIT_ALLOWED_REPOS`, defaulting to the backend and frontend workspace roots.
HTTP contract runs are constrained by `TOOL_HTTP_ALLOWED_HOSTS`, defaulting to `*`.
