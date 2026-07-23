# Tools

## Overview

Tools are resolved through two boundaries:

- app-owned built-ins under `app/tools/implementations/**`
- user-extensible integrations under `integrations/**`

The canonical layers are:

- `app/tools/config/agency_tools.yaml`
- `app/tools/builtins.py`
- `app/tools/system_runtime_families.py`
- `app/tools/system_runtime_helpers.py`
- `app/tools/system_specs.py`
- `app/tools/system_catalog.py`
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

## What Agents See

Agents are not given tool implementation code. For both native workflow agents and the main-agent chat path, Agency
builds an LLM function/tool payload from the persisted `ToolDefinition`:

```python
{
    "type": "function",
    "function": {
        "name": tool_call_name(tool),
        "description": tool.description,
        "parameters": tool.input_schema or {"type": "object"},
    },
}
```

So the model sees:

- callable tool name, for example `run_command`
- natural-language description
- JSON Schema parameters
- applicable system prompt or tool-use instructions from the agent context

It does not receive:

- Python source code
- implementation module body
- executor code
- credentials
- internal security logic

Tool and connector definitions may refer to backend environment credentials only when the variable name appears in
`AGENCY_CREDENTIAL_ENV_ALLOWLIST`. This keeps a user-authored definition from turning an arbitrary backend
environment variable into an accessible secret reference.

The implementation reference stays server-side in `ToolDefinition.implementation`, for example:

```python
implementation = {
    "implementation_type": "python_function",
    "target": "app.tools.implementations.browser",
    "callable_name": "open_browser",
}
```

When the model returns a tool call, it returns only the callable name and structured arguments:

```json
{
  "name": "open_browser",
  "arguments": {
    "url": "https://example.com"
  }
}
```

When the model returns a tool call, Agency resolves the callable name back to a stored `ToolDefinition`, validates the
arguments against `input_schema`, checks approval and policy, records audit events, then dispatches the server-side
executor. This keeps tool selection model-visible while implementation authority remains in the backend.

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

## Unified Browser Tools

`agency.browser.open` is the single entry point for both page retrieval and live browsing. With `keep_open=false` it
extracts content and closes browser resources before returning. With `keep_open=true` it returns an owner-scoped
`session_id` that the screenshot, scroll, click, type, select, verify, repeat-extraction, and close tools operate on.
Patchright is always the primary engine; Scrapling is an internal last resort rather than a model-selectable tool.

Agents may pass a nested `runtime_policy` to `agency.browser.open` with per-call session TTLs, session limits,
navigation timeout, retry attempts, domain concurrency/pacing, and artifact retention. These are preferences, not new
authority: the browser runtime clamps them to the local environment limits. See
[Unified Browser Capability](./unified-browser.md) for lifecycle, challenge handoff, and security details.

## Voice And Media Tools

Agency-owned voice generation and media delivery use separate tool boundaries:

```text
agency.voice.generate = create and store a reusable voice artifact
agency.media.publish = store any local media artifact for reuse
agency.media.send = deliver an existing media artifact to a selected tied application
```

Voice generation and media delivery are intentionally independent. Workflows can mix and match producers and delivery
targets without making voice generation depend on Discord, Telegram, Slack, Teams, WhatsApp, or any other tied
application.

`agency.voice.generate` is the first-class Agency voice generation tool. It accepts text, publishes generated audio
through Agency storage, and returns a reusable artifact with `file_path`, `storage_key`, `storage_uri`, `media_url`,
`content_type`, and `provider_fetchable` fields.

Supported voice provider values:

- `auto`: uses `openvoice_local` when a consented `reference_voice_path` is supplied, otherwise falls back to `system_tts`.
- `openvoice_local`: local OpenVoice package/checkpoint execution owned by Agency.
- `system_tts`: local OS or package TTS such as macOS `say`, `espeak-ng`, or `espeak`.

Local-first behavior is the default because local deployments may have restrictive data policies. Paid or cloud
providers must remain explicit installable adapters and should not be called implicitly by `auto`.

Agency runs OpenVoice directly from a local checkout. Set `AGENCY_OPENVOICE_ROOT` to the checkout path, or install
OpenVoice at `external/openvoice`. Set `AGENCY_OPENVOICE_CHECKPOINTS_DIR`, or place V1 checkpoints under
`external/openvoice/checkpoints`. Treat OpenVoice `.pth` checkpoints as trusted local runtime assets; do not point
Agency at checkpoints downloaded or modified by untrusted users.

Voice-generation guardrails:

- `ai_disclosure=true` is required for generated speech.
- `consent_confirmed=true` is required whenever a reference or cloned voice is used.
- Reference or cloned voice requests should include `reference_voice_path`.
- Generated files are stored through Agency storage, not through destination-specific delivery logic.

Example voice-generation payload:

```json
{
  "text": "Here is today's short learning summary.",
  "provider": "openvoice_local",
  "reference_voice_path": "/absolute/path/to/consented-reference.wav",
  "output_name": "daily-learning-summary.wav",
  "storage_key_prefix": "media/learning-coach",
  "purpose": "Daily learning coach voice summary for the owner.",
  "ai_disclosure": true,
  "consent_confirmed": true,
  "dry_run": false,
  "metadata": {
    "workflow_id": "workflow-agency-learning-decision-coach",
    "task_id": "generate-voice-lesson",
    "run_id": "example-run-id"
  }
}
```

`agency.media.publish` copies a local image, audio, voice, video, or document into Agency storage and returns a reusable
media artifact. Use it when a producer returns only a local path and later workflow steps need a shared `storage_uri` or
`media_url`.

`agency.media.send` owns provider-specific delivery to tied applications. It accepts media from voice generation, media
publication, image/video/document tools, or any other workflow step. Current provider aliases include `telegram`,
`discord`, `slack`, `microsoft-teams`, and `whatsapp`.

Most providers require a provider-fetchable `media_url`. Providers with upload adapters may accept `file_path` directly;
the current Discord adapter can upload a local file attachment. Workflows should pass both `media_url` and `file_path`
when both are available so each provider adapter can choose the supported path.

Example media-delivery payload:

```json
{
  "provider": "discord",
  "media_type": "voice",
  "media_url": "http://localhost:8000/api/local-storage/download/media/learning-coach/daily-learning-summary.wav",
  "file_path": "/app/local_storage/media/learning-coach/daily-learning-summary.wav",
  "text": "Today's summary: ...",
  "caption": "Agency Daily Learning Coach voice summary",
  "destination_id": "channel-id",
  "credential_id": "credential-id",
  "owner_user_id": "dev-user",
  "dry_run": false,
  "metadata": {
    "ai_disclosure": true
  }
}
```

Recommended workflow shape:

1. Generate or produce media with the appropriate producer tool.
2. Store or publish the artifact through Agency storage when needed.
3. Deliver the existing artifact with `agency.media.send` only if the workflow needs outbound delivery.

Do not make producer tools call destination-specific delivery tools internally. Producer tools should return reusable
artifacts; delivery tools should own destination-specific transport.

Direct tool-runtime execution supports `agency.voice.generate`, `agency.media.publish`, and `agency.media.send`.
Workflow definitions can assign these tool IDs to tasks, but workflow-run validation should verify that actual rows are
created in `tool_invocations`. A successful-looking workflow output is not enough to prove real media generation or
delivery occurred. Until workflow-native tool bridging is confirmed for a run, validate with:

```sql
select tool_id, status, output_json
from tool_invocations
where execution_id = '<run-id>'
order by started_at;
```

If `tool_invocations` is empty, the workflow runner has not invoked the real tool runtime for that execution.

Optional installable module packs are not loaded from Python entry points by default. Enable
`AGENCY_OPTIONAL_MODULE_ENTRY_POINTS_ENABLED=true` only when operators intentionally want installed
`agency.module_packs` packages to register routes or tools. Local/private modules can still be enabled explicitly with
`AGENCY_OPTIONAL_MODULE_SPEC_REFS`.

## Workflow Governance Tools

See also [`../generated_tools/README.md`](../generated_tools/README.md) for the coder-agent-authored shared tool
workspace under `generated_tools/`.

For operator-facing lifecycle management, `/tools/generated` now supports both package inspection and
authoring actions. Operators can scaffold package folders and publish generated callables into shared
`ToolDefinition` records without leaving the portal, while the backend service continues to enforce
callable existence and conservative security defaults.

Agency now exposes a small workflow-governance tool family for main-agent and workflow-operator
review loops. These tools are declared in `app/tools/config/agency_tools.yaml` and routed through
`WorkflowService`.

Current governance review tools:

- `agency.workflow.governance.audit`: inspect governance drift such as missing or orphaned approval links
- `agency.workflow.governance.review-queue`: aggregate proposals, steering approvals, evidence gaps,
  orphaned approvals, remediation suggestions, and derived record activity into one operator queue
- `agency.workflow.governance.document-suggest`: rank uploaded workflow documents that may serve as
  evidence for one governance record
- `agency.workflow.governance.bundle`: run a guarded multi-step governance flow for one record,
  usually suggest evidence, optionally attach the top match, then optionally request approval
- `agency.workflow.governance.act`: execute one discrete governance mutation for a workflow record

`agency.workflow.governance.act` currently supports these `action` values:

- `request_approval`
- `attach_evidence`
- `resolve`
- `dismiss`
- `reopen`
- `apply_remediation`

The lifecycle actions are intentionally conservative:

- `resolve` and `dismiss` are manual operator closures for records that do not currently have a
  linked approval request
- `reopen` reactivates only manually closed records
- records already linked to approval requests should move through approval state sync or governance
  repair, not manual lifecycle overrides

This keeps workflow-owned governance history from silently drifting away from the canonical approval
record stored in the approval subsystem.

## Input Ownership Contract

Built-in Agency tools can now annotate each input-schema property with parameter ownership metadata so
the workflow editor, runtime, and agents share the same contract.

- `x-agency-filled-by: "user"` means the workflow author is expected to configure the value ahead of runtime.
- `x-agency-filled-by: "agent"` means the value is supplied by the agent when the tool executes.
- `x-agency-filled-by: "user_or_agent"` means either path is valid depending on workflow setup.
- `x-agency-user-visible: false` hides runtime-managed or setup-hidden fields from ordinary workflow
  configuration UIs.

The canonical assembled builtin registry now flows through `app/tools/builtins.py`.
That module is the single entrypoint for "all builtin Agency tools" and is what CLI discovery,
seed data, and runtime inspection should call.

The current declaration layer is intentionally hybrid:

- YAML-owned registry metadata in `app/tools/config/agency_tools.yaml`
- Python-owned schema, implementation, and runtime wiring across the existing `app/tools/*` and `app/services/agent_tools.py` modules

`app/tools/config/agency_tools.yaml` is now the human-owned registry for app-tool metadata,
declarative system-tool specs, and builtin system-family policy metadata. `app/tools/registry_config.py`,
`app/tools/system_specs.py`, and `app/tools/system_catalog.py` all read that YAML and attach the
corresponding Python builders or compatibility constants. `app/tools/definitions.py` projects
app-tool metadata into each tool's `input_schema`. `app/tools/system_runtime_helpers.py` holds
shared helper schemas for memory and graph. `app/tools/system_runtime_families.py` holds the
concrete runtime-heavy memory and graph tool declarations. `app/services/agent_tools.py` now
mainly keeps ids, family gating, and shared schema-annotation logic.

The architectural rationale for that split is documented in
`docs/adr/0001-hybrid-tool-registry.md`.

For a quick assembled view of the final builtin registry, run:

```bash
python -m app.cli tool registry
python -m app.cli tool registry --json
make check-tool-registry
```

Current examples:

- `agency.file.write-text`: `base_folder` is workflow/user-owned, while `filename`, `content`, and
  `mode` are agent-filled runtime inputs.
- `agency.http.request`: request fields such as `url`, `method`, `headers`, `query_params`, `body`,
  `verify_ssl`, and `credential_mode` are declared as `user_or_agent` so a workflow can either save
  defaults or leave them for the agent to provide at execution time.

## MCP Exposure And Packaging Direction

Agency-native tools remain the canonical implementation and governance boundary. Tool definitions, permissions,
credential bindings, approval requirements, audit events, workflow ownership, and generated-tool metadata should stay in
Agency even when a capability is exposed to external runtimes. MCP is the interoperability boundary, not the internal
source of truth for every tool.

The preferred direction is to expose selected Agency-managed capabilities through an Agency MCP server. External
runtimes such as Codex, Claude Code, Cursor, or other MCP-compatible hosts should be able to discover and call approved
Agency tools without knowing Agency's internal registry, execution, approval, or credential models. The MCP server
adapter should call the existing Agency runtime executor rather than reimplementing tool behavior.

Tool exposure should be explicit and opt-in through `ToolDefinition.mcp_exposure`, for example:

- `expose_as_mcp_tool=true` for tools that are safe and useful outside the native Agency runtime
- optional `name_override` when the external callable name needs to differ from the internal `name`
- tags that help external runtimes group or filter capabilities
- security metadata that preserves approval, sandbox, redaction, and credential constraints at execution time

External project-mutation tools must be narrow and policy-backed. Prefer typed tools such as repository inspection,
generated-tool package authoring, workflow proposal, test execution, patch creation, and approval request creation.
Avoid exposing broad unrestricted shell or filesystem tools to external runtimes unless they are sandboxed,
approval-gated, and recorded through the same runtime event model as native Agency executions.

High-value tools may later move outside Agency as standalone MCP servers when they are useful beyond this project or
need an independent lifecycle. In that case, package the tool as its own implementation plus MCP wrapper, tests,
schemas, and installation metadata. Agency should then consume the standalone MCP server through the existing MCP client
path and project it back into normal `ToolDefinition` records.

This creates two supported directions:

```text
External runtime -> Agency MCP server -> Agency registry, approvals, workflows, and executors
Standalone MCP tool -> Agency MCP client -> ToolDefinition -> normal Agency execution policy
```

Do not convert every internal tool into a standalone MCP server by default. Premature standalone packaging creates extra
deployment, credential, versioning, and permission surfaces. Promote a tool out of Agency only when the capability has a
clear audience outside Agency, a stable schema, meaningful tests, and a reason to be versioned independently.

## Future Integration Tooling TODOs

Current connector tooling supports repeated credential instances through credential identity metadata, resolver tooling,
tool/workflow connector bindings, structured target-scope metadata, and runtime interpolation for `agency.http.request`.
Telegram, Discord, and WhatsApp also support conversation-bound delivery targets. Slack, Microsoft Teams, Twilio SMS,
Gmail, and Outlook currently have outbound-only adapter delivery support through
`/integrations/conversations/adapters/{provider}/deliver`.

Backend and frontend provider selection must use canonical connector provider keys from the connector capability
registry, for example `discord-bot`, `telegram-bot`, and `whatsapp-cloud-api`. Tool definitions may carry aliases such
as `implementation.config.provider = "discord"`, but workflow validation, runtime binding resolution, and the frontend
tool drawer must normalize those aliases before matching credentials. Connector binding controls should only render for
tools that resolve to a known connector provider or already have an explicit binding.

The registry may expose `instanceIdentityMetadata` and `targetScopeMetadata` for planned providers before the adapter is
fully production-ready. Treat those fields as resolver and UI hints until that provider also has concrete action schemas,
OAuth/scope requirements, target pickers, allowlists, webhook/inbound validation where applicable, and adapter/runtime
tests.

The remaining integrations need the following work before agents can reliably choose, bind, and operate them end to end:

| Integration          | Future required work                                                                                                                                                                                                         |
|----------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Slack                | Add conversation-bound channel type support, inbound event/webhook verification, workspace/team allowlists, channel picker/target UI, OAuth scope schema, and tests for channel delivery plus command/approval callbacks.    |
| Microsoft Teams      | Add conversation-bound channel type support, Graph webhook validation, tenant/team/channel target UI, OAuth scope schema, and tests for channel delivery plus approval callbacks.                                            |
| Twilio SMS           | Add conversation-bound SMS target support, inbound webhook validation/signature checks, sender/recipient allowlists, phone-number target UI, and tests for bidirectional SMS delivery.                                       |
| Gmail                | Add structured email composition UI/tool schema instead of raw MIME-only payloads, mailbox/delegated-user selection, OAuth scope schema, recipient allowlists, and tests for send/draft/search actions.                      |
| Outlook              | Add structured email composition UI/tool schema, mailbox/shared-mailbox selection, Microsoft Graph scope schema, recipient allowlists, and tests for send/draft/search actions.                                              |
| Notion               | Define concrete database/page target schemas, OAuth scope requirements, workspace/database/page picker support, action schemas for search/create/update/publish, and resolver tests for multiple workspaces.                 |
| Linear               | Define workspace/team/project target schemas, OAuth/API-token scope requirements, issue/project action schemas, and resolver tests for multiple teams/projects.                                                              |
| Jira                 | Define Atlassian site/project/issue target schemas, OAuth scope requirements, project picker support, issue action schemas, and resolver tests for multiple sites/projects.                                                  |
| Confluence           | Define Atlassian site/space/page target schemas, OAuth scope requirements, space/page picker support, page/search action schemas, and resolver tests for multiple sites/spaces.                                              |
| Airtable             | Define workspace/base/table target schemas, token scope requirements, base/table picker support, record action schemas, and resolver tests for multiple bases.                                                               |
| Google Workspace     | Split shared suite metadata into concrete Drive, Calendar, Docs, and Sheets action schemas; define domain/customer/calendar/drive target scopes, OAuth scopes, and resolver tests for multiple domains/drives/calendars.     |
| Microsoft 365        | Split shared suite metadata into SharePoint, OneDrive, Outlook, Teams, and Calendar action schemas; define tenant/site/drive/mailbox/team target scopes, Graph scopes, and resolver tests for multiple tenants/sites/drives. |
| GitHub               | Define repository/project/branch target schemas, GitHub App installation scope requirements, repo/owner picker support, issue/PR/repo action schemas, and resolver tests for multiple installations.                         |
| GitLab               | Define namespace/project/branch target schemas, token scope requirements, project picker support, issue/MR/repo action schemas, and resolver tests for multiple projects.                                                    |
| Sentry               | Define organization/project/environment target schemas, token scope requirements, issue/event action schemas, and resolver tests for multiple organizations/projects.                                                        |
| PagerDuty            | Define account/service/escalation-policy target schemas, token scope requirements, incident/action schemas, and resolver tests for multiple services.                                                                        |
| Figma                | Define team/project/file target schemas, OAuth scope requirements, file picker support, comment/review action schemas, and resolver tests for multiple teams/files.                                                          |
| Canva                | Define team/folder/design target schemas, OAuth scope requirements, design/folder picker support, publishing/review action schemas, and resolver tests for multiple teams/folders.                                           |
| YouTube              | Define channel/playlist/video target schemas, OAuth scope requirements, channel picker support, upload/comment/analytics action schemas, and resolver tests for multiple channels.                                           |
| Adobe Creative Cloud | Define organization/project/library/asset target schemas, OAuth scope requirements, asset picker support, review/export action schemas, and resolver tests for multiple organizations/projects.                              |
| S3                   | Define AWS account/region/bucket/prefix target schemas, IAM policy requirements, bucket/prefix picker support, object action schemas, and resolver tests for multiple buckets/accounts.                                      |
| Google Drive         | Define domain/drive/folder target schemas, OAuth scope requirements, drive/folder picker support, file action schemas, and resolver tests for multiple drives/folders.                                                       |
| Dropbox              | Define team/namespace/folder target schemas, OAuth scope requirements, namespace/folder picker support, file action schemas, and resolver tests for multiple namespaces.                                                     |
| OneDrive             | Define tenant/drive/folder target schemas, Microsoft Graph scope requirements, drive/folder picker support, file action schemas, and resolver tests for multiple drives.                                                     |
| SharePoint           | Define tenant/site/drive/folder target schemas, Microsoft Graph scope requirements, site/library picker support, file/page action schemas, and resolver tests for multiple sites.                                            |
| Perplexity           | Define account, workspace, and search-policy metadata, domain allowlist/denylist schema, query action schema, rate-limit policy, and resolver tests for multiple accounts.                                                   |
| Tavily               | Define account, workspace, and search-policy metadata, domain allowlist/denylist schema, query action schema, rate-limit policy, and resolver tests for multiple accounts.                                                   |

Cross-cutting requirements for all of these integrations:

- Add provider-specific `instanceIdentityMetadata`, `targetScopeMetadata`, OAuth scopes, and action capability names to
  the connector registry.
- Add frontend setup forms and workflow binding fields from those schemas, avoiding raw JSON where concrete schemas
  exist.
- Ensure agents call `agency.connector.resolve` before proposing connector-backed workflows when multiple credentials
  match a provider.
- Validate proposed connector-backed tools/workflows before approval so missing bindings are caught earlier than runtime,
  and keep native runtime validation as the final fail-closed guard before delivery.
- Add resolver tests for repeated installations and request-shape tests for each concrete action.
- Add conversation-bound delivery only when the domain has a real inbound identity model, webhook verification, and
  saved target fields.

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

`agency.voice.generate`, `agency.media.send`, `agency.http.request`, and `agency.file.write-text` are explicit autonomous
exceptions: they set `requires_approval: false` so workflows can invoke them without a durable approval pause. They
remain sandboxed and continue to enforce their path, domain, connector, credential, input-schema, and callable
allowlists. This exception applies globally to the canonical built-in definitions, not to one workflow only.

## Approval Model

Approval-gated tools are expected to:

1. declare their approval requirement in tool metadata
2. trigger an approval request before execution
3. pause the execution until approval or rejection
4. emit canonical execution events and invocation records for the decision

The approval model is enforced in the runtime layer, not inside arbitrary tool code. The approval request row is the
decision source of truth, and a linked `execution_waits` row records the execution's durable wait state. Before a gated
native tool suspends, the agent executor persists the transcript and exact pending tool position. The worker then exits;
an API process can approve or reject the request, atomically wake the execution, and queue a fresh worker that consumes
the decision and continues without replaying prior calls in that model response.

The synchronous `agency.human.ask` channel remains suitable for short, live interactions; it is not the durable source
of truth for long-lived workflow suspension.

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

## Agency Graph Tools

Agency Graph tools are read-only tools for navigating durable graph projection and building bounded runtime context. They
are controlled by `AGENCY_GRAPH_CONTEXT_TOOLS_ENABLED`, which defaults to enabled for local/manual use. When disabled,
the graph tool family is omitted from built-in discovery, generated contracts, and seed data.

Available graph tools:

- `agency.graph.context`: synthesize bounded context for resume, debug, steering, planning, audit, handoff, learning, or
  root-cause workflows.
- `agency.graph.search`: search graph nodes by text and canonical filters to find candidate anchors.
- `agency.graph.expand`: expand a bounded neighborhood around a node using explicit labels/relationships, a preset, or a
  graph mode.
- `agency.graph.neighbors`: return the immediate grouped neighbors for a node.
- `agency.graph.path`: answer predefined path questions such as shortest paths, memory-to-run provenance, failed-run
  root cause, influence, and prior agent runs.
- `agency.graph.summarize-subgraph`: synthesize a deterministic context response from a caller-provided bounded graph
  payload.
- `agency.graph.working-set.create`, `agency.graph.working-set.add`, `agency.graph.working-set.remove`,
  `agency.graph.working-set.summarize`, and `agency.graph.working-set.clear`: maintain a temporary in-runtime graph
  working set for anchors, visited nodes, selected nodes, and notes.
- `agency.graph.working-set.persist-context-pack`: persist a curated working set as a durable `memory_type=context_pack`
  when the runtime state and memory policy allow it.

Graph reads are bounded by `AGENCY_GRAPH_CONTEXT_QUERY_TIMEOUT_SECONDS`, which defaults to five seconds. Repeated
traversal is also bounded per actor by `AGENCY_GRAPH_CONTEXT_RATE_LIMIT_WINDOW_SECONDS` and
`AGENCY_GRAPH_CONTEXT_RATE_LIMIT_MAX_UNITS`; cost is based on requested limit, anchor depth, and budget mode.

The tool accepts either an anchor pair or a query:

- `anchor_type` plus `anchor_id` for a known workflow, run, execution, agent, task, step run, tool, memory, context pack,
  document, entity, error, conversation, message, model request, or approval request
- `query` for graph search, with optional `scope.labels` or `scope.node_types` to narrow node labels
- `scope.runtime_context` or direct scope ids such as `execution_id`, `run_id`, `task_id`, `agent_id`, or `workflow_id`
  when the current runtime already knows the relevant anchor

Additional inputs include `intent`, `mode`, `include_memories`, `include_events`, `include_raw_graph`, `budget`, and
`limit`. Budgets are `brief`, `balanced`, `full`, and `raw_graph`; raw graph output is still bounded and is only included
when requested or when the raw-graph budget is selected.

The result includes `summary`, deterministic `facts`, related memories/documents/events, prior attempts, failures,
decisions, constraints, open questions, next actions, provenance, omitted counts, optional bounded graph payload, and
query metadata. The tool does not mutate source records and does not expose arbitrary Cypher. If graph projection is
disabled or unavailable, it returns a structured status with guidance to use durable memory, execution events, a clearer
anchor, projection/backfill, or a narrower query after timeout or budget exhaustion.

Search, expand, neighbors, and path tools return normalized graph DTO payloads with `nodes`, `edges`, and `meta`. They
are useful when an agent needs to navigate before synthesizing context: search finds candidate anchors, expand and
neighbors explore local structure, and path answers bounded predefined relationship questions without exposing Cypher.
Working-set tools operate only on native runtime state; they help an agent keep track of a subgraph across multiple
steps without changing source graph projection records. Persisting a working set writes a durable context-pack memory,
not graph nodes or relationships.

Agency Graph is the shared operational graph model. Sigma is a graph visualization consumer over normalized graph DTOs.
Agents should query Agency Graph through tool contracts such as `agency.graph.context`, `agency.graph.search`, and
`agency.graph.path`; they should not depend on Sigma UI state or Sigma-specific layouts to understand memory or runtime
relationships.

Every `agency.graph.context` runtime call also emits an `agency.graph.context.completed` event. The event metadata includes tool id, actor,
status, intent, mode, anchor, depth, limit, budget, node and edge counts, omitted counts, duration, graph availability,
fallback state, and traversal budget fields so graph-context behavior can be inspected without enabling raw graph output.

Graph context output is defensively redacted before it reaches agents. Sensitive property keys such as tokens,
passwords, credentials, secrets, authorization values, raw content, and embeddings are removed from raw graph payloads.
Nodes marked `sensitive=true` keep provenance and type information but hide display text such as summaries, content,
messages, titles, and labels. Long string properties are truncated before raw graph output is returned.
Credential, token, auth-session, connector-account, and external-account nodes are omitted entirely with their connected
edges. Integration and connector nodes remain visible only as protected placeholders with safe health/status/provenance
metadata; names, configs, credential refs, account ids, and scopes are not returned.

Memory nodes are also filtered through durable-memory policy when the graph node can be matched to a `MemoryRecord`.
The runtime actor is passed as `scope.runtime_context.current_user_id`; memory nodes the actor cannot read are removed
with their connected edges. Sensitive memories are hidden unless the scope explicitly sets
`include_sensitive_memories=true`, and workflow/agent/task/conversation/run memory exclusions remove matching memory
nodes from context and raw graph output.

### Agency Graph Examples

Main agent steering a stalled sub-agent:

```json
{
  "intent": "steer",
  "anchor_type": "execution",
  "anchor_id": "execution-123",
  "include_events": true,
  "budget": "balanced"
}
```

Use the returned failures, prior attempts, decisions, and next actions to decide whether to send a steering message,
request human input, or inspect exact execution events.

Coding agent resuming prior work:

```json
{
  "intent": "resume",
  "scope": {
    "runtime_context": {
      "task_id": "task-implement-graph-tools",
      "workflow_id": "workflow-agency-graph"
    }
  },
  "include_events": true,
  "budget": "balanced"
}
```

Use the response to avoid repeated failed approaches, recover constraints and decisions, and identify the next concrete
implementation step before running `agency.command.run`.

Debugging a failed workflow run:

```json
{
  "intent": "root_cause",
  "anchor_type": "run",
  "anchor_id": "run-failed-import",
  "include_events": true,
  "budget": "brief"
}
```

If graph projection has not caught up, fall back to `agency.execution.events` for exact event logs.

Tracing memory provenance:

```json
{
  "intent": "audit",
  "anchor_type": "memory",
  "anchor_id": "memory-decision-42",
  "mode": "lineage",
  "budget": "balanced"
}
```

Inspect provenance nodes and edges to see which run, document, conversation, or context pack produced the memory.

Creating a context pack from graph context:

```json
{
  "execution_id": "execution-123",
  "working_set_id": "graph-working-set-123",
  "scope": "workflow",
  "workflow_id": "workflow-agency-graph",
  "summary": "Graph context for stalled sub-agent recovery",
  "confirmed": true
}
```

Use `agency.graph.working-set.persist-context-pack` only after the working set has been curated. Sensitive graph nodes
force sensitive memory handling and may require explicit confirmation.

Operator-selected Sigma or Agency Graph node:

```json
{
  "intent": "handoff",
  "anchor_type": "task",
  "anchor_id": "task-42",
  "budget": "balanced"
}
```

When UI support passes a selected node id to the main agent, use that id as the graph anchor and keep raw graph output
disabled unless the operator asks to inspect the DTO.

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
