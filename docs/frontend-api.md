# Frontend API

## Overview

`open-agency` owns the canonical backend API under `app/api`. Browser and server-side callers in `open-agency-fe` should prefer
calling those runtime and catalog routes directly whenever the frontend does not need to adapt the authenticated session
or reshape the request for UI-specific concerns.

Current contract status:

- direct backend reads and straightforward mutations should use the configured Agency runtime origin
- explicit frontend BFF routes are reserved for session adaptation, UI aggregation, marketplace browsing, or streaming adaptation
- runtime-specific mapping, including CrewAI field translation, happens at the backend adapter boundary
- execution creation, runtime-adapter fallback, and execution-host handling remain backend-owned behavior even when a frontend BFF route initiates the request
- migration notes and retired compatibility inventories are not part of the maintained API guidance

Primary frontend route groups:

- `/agents`
- `/tools`
- `/tools/contracts`
- `/tools/{tool_name}/run`
- `/model-providers`
- `/model-profiles`
- `/mcp-servers`
- `/runtime-adapters`
- `/workflows`
- `/executions`
- `/goals`
- `/schedules`
- `/observability/*`
- `/a2a/*`

Use `/executions` plus workflow start routes for workflow launches, and `/executions/{execution_id}/*` for status,
events, approvals, artifacts, and control actions.

Use `GET /goals/operator-view` for the goals workspace list projection. It returns each goal's status, objective,
priority, deadline, current plan, active executions, next supervisor action, blocked reason, pending approvals,
automatic main-agent actions, and stale/failing/missing-evidence flags. Use `GET /goals/{goal_id}/operator-detail` for
the selected-goal projection with timeline entries, evidence, execution artifacts, approval records, memory ids,
evaluation results, supervisor findings, supervisor decisions, and recorded supervisor actions. Mutations remain on the
canonical goal endpoints such as `PATCH /goals/{goal_id}`, `POST /goals/{goal_id}/pause`, `resume`, `cancel`,
`evidence`, `evaluate`, and `complete`. Operator workspaces may use `POST /goals/{goal_id}/operator-actions` for a
single action surface covering `pause`, `resume`, `cancel`, `adjust_autonomy`, `update_success_criteria`, and
`reassign`; approval records returned from the operator projections should link to the existing conversation approval
routes for approve/reject/request-changes decisions.

Frontend chat may pass selected goal context in conversation message metadata as `goal_id`, `goal_mentions`, and
`goal_intent`. Workflow launch surfaces should pass `goal_id` through execution input, trigger metadata, and the
execution creation payload so the backend links the run as an attempt under the durable goal.

Use `/tools` for canonical tool discovery and CRUD. Tool records expose stable `id`, callable `name`, and human-facing
`display_name`. Use `/tools/contracts` for machine-readable execution contracts and `/tools/{tool_name}/run` for
contract-mediated execution.

## Key Endpoints

Catalog endpoints:

- `GET /agents`
- `POST /agents`
- `GET /agents/import/formats`
- `POST /agents/import/preview`
- `POST /agents/import/commit`
- `POST /agents/import/batch-preview`
- `POST /agents/import/batch-commit`
- `GET /tools`
- `POST /tools`
- `GET /tools/contracts`
- `GET /tools/contracts/{tool_name}`
- `POST /tools/{tool_name}/run`
- `GET /model-providers`
- `POST /model-providers`
- `GET /model-profiles`
- `POST /model-profiles`
- `POST /documents/intelligence`
- `POST /documents/ingest`
- `GET /documents`
- `GET /documents/{document_id}`
- `DELETE /documents/{document_id}`
- `GET /memories`
- `POST /memories`
- `GET /memories/source-intelligence/catalog`
- `POST /memories/source-intelligence/analyze`
- `POST /memories/embeddings/backfill`
- `POST /memories/daily-summaries/run`
- `POST /memories/daily-summaries/backfill`
- `PATCH /memories/{memory_id}`
- `PATCH /memories/{memory_id}/source-intelligence`
- `DELETE /memories/{memory_id}`
- `GET /persona`
- `POST /persona`
- `GET /persona/{persona_id}`
- `PATCH /persona/{persona_id}`
- `DELETE /persona/{persona_id}`
- `GET /persona/{persona_id}/versions`
- `GET /persona/{persona_id}/graph-context`
- `GET /persona/{persona_id}/workflow-usages`
- `GET /persona/{persona_id}/sources`
- `POST /persona/{persona_id}/sources`
- `GET /persona-factory/governance-labels`
- `GET /persona-factory/item-types`
- `POST /persona-factory/distill`
- `GET /persona-factory/runs`
- `GET /persona-factory/runs/{run_id}`
- `GET /persona-factory/runs/{run_id}/items`
- `PATCH /persona-factory/items/{item_id}`
- `POST /persona-factory/items/{item_id}/approve`
- `POST /persona-factory/items/{item_id}/reject`
- `POST /persona-factory/runs/{run_id}/synthesize-package`
- `PATCH /persona-factory/runs/{run_id}/package`
- `POST /persona-factory/runs/{run_id}/approve`
- `POST /persona-factory/runs/{run_id}/publish`
- FE route: `/memory` provides manual memory CRUD for humans outside chat.
- FE route: `/persona` should provide the Persona Factory workspace for source selection, governance labels, draft
  review, approval, and publish. `open-agency-fe` is optional; the backend supports the same lifecycle through API/CLI calls.
- Document upload intelligence accepts multipart uploads through `POST /documents/intelligence`, uses the active
  main-agent model profile when available, and recommends document kind, memory scope, optional binding, tags, chunking,
  and persona governance labels before ingestion.
- Document ingestion accepts multipart uploads with `upload_mode=vector|context|both`. Missing `upload_mode` behaves as
  `vector` for backward compatibility. `vector` creates `archive` memory chunks with document provenance metadata;
  `context` creates an uploaded-document reference for the immediate conversation turn without creating archive chunks;
  `both` creates both. Responses include `upload_mode`, `estimated_tokens`, and `context_attachment_id` when the file can
  be attached to a message.
- `POST /documents/ingest` can also run the same upload intelligence path when `auto_intelligence=true`, storing the
  recommendation and applied settings in chunk metadata and uploaded-document metadata.
- Direct-context uploads should add the returned `context_attachment_id` to the user message metadata as
  `context_attachment_ids`. The backend loads that document text as untrusted source context for the latest turn only and
  keeps the extracted text out of conversation message rows.
- Uploaded-document lists should use `GET /documents` as the primary source so context-only documents are visible.
  Memory-backed chunk grouping can still be used as a legacy fallback. `DELETE /documents/{document_id}` tombstones
  context-only documents and clears extracted text; for `vector` and `both`, it also deletes related archive chunks.
  UI labels should distinguish `Context only`, `Retrieval`, and `Context + retrieval`.
- All upload surfaces should use this shared document intelligence contract, including Assistant/conversation uploads,
  Memory Ops, Agent documents, Workflow documents, graph task or memory-node uploads, Tool-related knowledge surfaces,
  and Persona Factory. Context-specific UIs can lock known scope or bindings while still letting the main agent recommend
  tags and chunking.
- Memory source intelligence lets `/operations/memory` analyze selected chunks before reuse. The review payload stores
  source classification, document kind, extraction targets, vector tags, and graph hints in memory metadata.
- Memory records may include embedding metadata: `embedding_model_profile_id`, `embedding_model`,
  `embedding_dimensions`, and `embedded_at`.
- Memory list filters also support summary-oriented fields such as `memory_type`, `status`,
  `source_conversation_id`, `source_execution_id`, `summary_date_from`, and `summary_date_to`.
- `POST /model-providers/{provider_id}/authorize`
- `POST /model-providers/{provider_id}/callback-complete`
- `POST /model-providers/{provider_id}/device-authorize`
- `POST /model-providers/{provider_id}/device-complete`
- `GET /runtime-adapters`
- `GET /mcp-servers`
- `GET /.well-known/agent-card.json`
- `POST /a2a/tasks`
- `GET /a2a/tasks/{task_id}`
- `POST /a2a/tasks/{task_id}/messages`
- `GET /a2a/tasks/{task_id}/artifacts`

Tool records expose a split identity contract. `id` is the stable persisted identity, `name` is the callable-safe
agent/runtime name, and `display_name` is the frontend label. UI code should render `display_name` through the frontend
tool display helper and should not show raw implementation/callable metadata in normal user-facing views.

Agent Markdown import endpoints provide preview-first ingestion for external `.md` agent files. Frontend callers should
preview uploaded, pasted, or URL-sourced Markdown, render warnings and conflicts for review, then commit only the
reviewed proposal. Tool and handoff suggestions are not granted unless the commit request explicitly approves them.
See `docs/agents-md-import.md` for the full import contract, security model, audit behavior, CLI usage, and deferred
work.

Persona Factory endpoints provide preview-first generation for reusable persona packages. CLI or frontend callers should
select or upload source memory, choose a distillation mode, call `POST /persona-factory/distill`, render generated
identity/persona/governance/package sections for review, persist edits through
`PATCH /persona-factory/runs/{run_id}/package`, approve the reviewed package, then publish it. Publishing creates a
versioned persona package, materializes an Agent definition, and writes persona-scoped durable memories with provenance.
Other ecosystems may call a similar package a "skill"; Agency keeps `Persona` as the canonical API and documentation
term. Backend-only creation and maintenance guidance lives in `docs/persona-factory.md`; see
`docs/persona-factory-cli.md` for curl examples that do not require `open-agency-fe`. Recurring operator steps live in
`docs/runbook.md`.

Persona Factory UI controls should derive mode options, defaults, enabled states, model profiles, LLM model sources, and
operational limits from `GET /persona-factory/item-types`. The mode selector should support `deterministic`, `llm`, and
`hybrid`; model-source controls should support `main_agent`, `model_profile`, and inline provider/model values when the
selected mode is LLM-backed. Existing run detail views must tolerate missing LLM metadata: legacy and deterministic runs
return `distillation_mode="deterministic"` and `llm_model_source=null`, while `resolved_model_*`,
`distillation_metrics.llm_distillation`, extraction-source counts, review flags, and conflict groups may be absent or
empty.

Hybrid and LLM review views should call `GET /persona-factory/runs/{run_id}/review-summary`, filter item lists by
`extraction_source`, `distiller`, `review_flag`, and `conflict_group_id`, and use
`POST /persona-factory/runs/{run_id}/review-actions` for reviewer decisions such as prefer LLM, prefer deterministic,
manual merge, or mark evidence insufficient.

Dedicated frontend component coverage should be added when a Persona Factory frontend package is present in this repo or
identified as the owning UI. The expected coverage is mode/model selection, legacy runs with missing LLM metadata,
review-summary filters, evidence display, conflict groups, and review actions.

`GET /persona/{persona_id}/graph-context` returns the same reviewed graph context prompt shape used by runtime persona
invocation when graph auto-retrieval is enabled. Frontends can use it for inspection/debug views, but CRUD and publish
flows should not require it.

Workflow and execution endpoints:

- `GET /workflows`
- `POST /workflows`
- `GET /workflows/{workflow_id}`
- `GET /workflows/{workflow_id}/shared-memory`
- `PATCH /workflows/{workflow_id}/shared-memory`
- `GET /workflows/{workflow_id}/persona-version-notices`
- `POST /workflows/{workflow_id}/persona-agents/{agent_id}/use-latest`
- `POST /workflows/{workflow_id}/persona-agents/{agent_id}/keep-current`
- `GET /workflows/{workflow_id}/versions`
- `GET /workflows/{workflow_id}/versions/{revision}`
- `POST /workflows/validate`
- `POST /executions`
- `POST /workflows/{workflow_id}/executions/start`
- `GET /executions`
- `GET /executions/{execution_id}`
- `GET /executions/{execution_id}/events`
  - Supports `after_sequence`
  - Supports repeated `event_type` filters and comma-separated `event_types`, for example
    `/executions/{id}/events?event_type=token.budget.warning&event_type=context.compaction.completed`
- `GET /executions/{execution_id}/stream`
- `GET /executions/{execution_id}/hitl/stream`
- `POST /executions/{execution_id}/hitl/reply`
- `GET /executions/{execution_id}/artifacts`
- `POST /executions/{execution_id}/cancel`
- `POST /schedules`
- `GET /schedules`

Observability and health:

- `GET /health`
- `GET /health/db`
- `GET /observability/executions/{execution_id}/timeline`
- `GET /observability/agents/{agent_id}/metrics`
- `GET /observability/workflows/{workflow_id}/metrics`
- `GET /observability/models/usage`
  - Supports optional `workflow_id`, `agent_id`, `execution_id`, `provider`, and `model` filters.

Agency Graph read endpoints:

- `GET /graph/read/status`
- `GET /graph/read/search`
- `GET /graph/read/nodes/{node_id}`
- `GET /graph/read/nodes/{node_id}/neighborhood`
- `GET /graph/read/nodes/{node_id}/expand`
- `GET /graph/read/nodes/{node_id}/neighbors`
- `GET /graph/read/workflows/{workflow_id}/neighborhood`
- `GET /graph/read/runs/{run_id}/neighborhood`
- `GET /graph/read/agents/{agent_id}/neighborhood`
- `GET /graph/read/tools/{tool_id}/neighborhood`
- `GET /graph/read/memories/{memory_id}/neighborhood`
- `GET /graph/read/entities/{entity_id}/neighborhood`
- `GET /graph/read/tasks/{task_id}/neighborhood`
- `GET /graph/read/workflows/{workflow_id}/lineage`
- `GET /graph/read/paths/shortest`
- `GET /graph/read/paths/memory-source-run`
- `GET /graph/read/paths/failed-run-root-cause`
- `GET /graph/read/paths/influence`
- `GET /graph/read/paths/agent-prior-runs`
- `GET /graph/read/presets/{preset}`

These routes return normalized `nodes`, `edges`, and `meta` DTOs. They are for UI and backend graph consumers; agents
should use read-only tool contracts such as `agency.graph.context` instead of depending on frontend visualization state.

## Workflow Builder Flow

Typical frontend flow for workflow authoring:

1. Load tool, agent, model profile, and runtime adapter catalogs.
2. Create or update a workflow definition.
3. Keep `native` as the default runtime unless there is a clear reason to allow another adapter.
4. Persist a versioned workflow through the workflows API.
5. Optionally allow `crewai` or another adapter for workflows that should support alternate runtimes.
6. Publish or activate the desired version.
7. Trigger a test execution against the selected runtime adapter.

The workflow domain definition is the contract between the builder UI, persistence, and runtimes.

Shared-memory controls:

- Use `GET /workflows/{workflow_id}/shared-memory` to load the workflow shared-memory operator payload.
- Use `PATCH /workflows/{workflow_id}/shared-memory` with `enabled`, optional `limit_per_layer`, and optional
  `apply_to_agents=true` to update shared-memory settings without submitting a full workflow definition.
- List workflow memory rows with `GET /memories?scope=workflow&workflow_id={workflow_id}`.

Persona-backed workflow agents:

- Workflows embed `agent_definitions`, including persona-generated agents. The embedded copy is the runtime snapshot
  used by that workflow until the workflow is updated.
- Persona-backed workflow agents are identified by `agent.metadata.persona_id`, `agent.metadata.persona_slug`, and
  `agent.metadata.persona_version_id`. Persona Factory generated agents also set
  `agent.metadata.generated_from_persona_factory=true`.
- Use `GET /persona/{persona_id}/workflow-usages` to list workflows that embed agents generated from a persona and see
  whether each workflow is `current`, `outdated`, or intentionally `pinned`.
- Use `GET /workflows/{workflow_id}/persona-version-notices` to show only actionable persona update notices for one
  workflow.
- Use `POST /workflows/{workflow_id}/persona-agents/{agent_id}/use-latest` to replace the embedded workflow agent with
  the currently published persona agent while preserving workflow-local graph metadata.
- Use `POST /workflows/{workflow_id}/persona-agents/{agent_id}/keep-current` to record that the workflow should keep its
  current persona snapshot until a newer persona version is published.
- Frontends should treat `outdated` notices as operator decisions, not automatic migrations. Show both actions: apply the
  latest persona package or keep the current workflow snapshot.

Typical notice payload:

```json
{
  "workflow_id": "workflow-1",
  "workflow_name": "Review Workflow",
  "items": [
    {
      "workflow_id": "workflow-1",
      "workflow_name": "Review Workflow",
      "agent_id": "persona-agent-audit",
      "agent_name": "audit-manager",
      "persona_id": "persona-audit",
      "persona_slug": "audit-manager",
      "workflow_persona_version_id": "version-1",
      "current_persona_version_id": "version-2",
      "persona_version": "1.0.0",
      "current_persona_version": "1.1.0",
      "published_agent_id": "persona-agent-audit",
      "status": "outdated",
      "message": "@audit-manager has a newer published persona version 1.1.0. This workflow uses 1.0.0.",
      "actions": {
        "use_latest": "/workflows/workflow-1/persona-agents/persona-agent-audit/use-latest",
        "keep_current": "/workflows/workflow-1/persona-agents/persona-agent-audit/keep-current"
      }
    }
  ],
  "count": 1,
  "has_updates": true
}
```

Workflow version history:

- `GET /workflows/{workflow_id}/versions` returns reverse-revision ordered items with `revision`, semantic `version`,
  `status`, `is_current`, `is_published`, timestamps, provenance, and the persisted `definition`.
- `GET /workflows/{workflow_id}/versions/{revision}` returns one persisted revision or `404` when the workflow or revision
  does not exist.

## Execution Control Flow

Typical execution lifecycle:

1. Frontend submits `POST /executions` with workflow reference, runtime adapter, and input payload.
2. Backend creates an execution record and initial execution events.
3. Runtime adapter runs the workflow and appends events, artifacts, approvals, and tool invocations.
4. Frontend polls execution detail or event endpoints to render progress.
5. Final output and status are read from the execution record and event stream.

### Execution Runtime Governance

`GET /executions/{execution_id}` includes a governance snapshot at `state.runtime_governance` for native runtime
executions. The shape is intentionally read-oriented and may contain:

```json
{
  "state": {
    "runtime_governance": {
      "token_usage": {
        "total": {
          "prompt_tokens": 100,
          "completion_tokens": 40,
          "total_tokens": 140,
          "estimated_cost": 0.00018,
          "currency": "USD"
        },
        "by_agent": {
          "agent-1": {"total_tokens": 140}
        },
        "by_task": {
          "task-1": {"total_tokens": 140}
        },
        "by_model": {
          "openai:gpt-4.1-mini": {"total_tokens": 140}
        }
      },
      "context_health": {
        "last": {
          "status": "normal",
          "estimated_prompt_tokens": 1200,
          "reserved_completion_tokens": 1024,
          "estimated_total_context_tokens": 2224,
          "context_window": 128000,
          "usage_ratio": 0.017375
        }
      },
      "context_compaction": {
        "last": {
          "compacted": true,
          "reason": "context_health_threshold",
          "memory_id": "optional-context-pack-id",
          "estimated_tokens_saved": 1800
        }
      },
      "supervision": {
        "pending_requests": [
          {
            "category": "token_budget_exceeded",
            "severity": "critical",
            "recommended_action": "request_replan",
            "status": "requested",
            "finding_event_id": "monitor-finding-event-id",
            "event_id": "supervisor-steering-event-id"
          }
        ],
        "last_steering_request_event_id": "supervisor-steering-event-id"
      }
    }
  }
}
```

Run detail may use narrower governance endpoints instead of pulling the full execution detail snapshot on each refresh:

- `GET /executions/{execution_id}/usage`
- `GET /executions/{execution_id}/context-usage`

For conversation direct replies and conversation compaction LLM calls, governance events and snapshots are written to the
conversation audit execution id `conversation-audit-{conversation_id}`. Frontend surfaces that inspect conversation
governance can call these same execution endpoints with that audit execution id. Use event payload `call_kind` values
such as `direct_reply` and `conversation_compaction` to separate chat responses from context-pack generation.

For CrewAI runs that use Agency model profiles, `AgencyModelClientLLM` emits the same governance event family with
`call_kind=crewai_bridge` and `metadata.source=crewai_llm_bridge`. CrewAI log replay may still emit lower-fidelity
`llm.*` events with `metadata.source=crewai_log`; prefer bridge events for token and context usage displays.

`GET /executions/{execution_id}/usage` returns token totals and budget warning history:

```json
{
  "execution_id": "execution-1",
  "workflow_id": "workflow-1",
  "source": "execution.metadata.runtime_governance",
  "token_usage": {
    "total": {
      "prompt_tokens": 100,
      "completion_tokens": 40,
      "total_tokens": 140,
      "estimated_cost": 0.00018,
      "currency": "USD"
    },
    "by_agent": {"agent-1": {"total_tokens": 140}},
    "by_task": {"task-1": {"total_tokens": 140}},
    "by_model": {"openai:gpt-4.1-mini": {"total_tokens": 140}}
  },
  "budget_warnings": [],
  "updated_at": "2026-05-25T06:00:00Z"
}
```

`GET /executions/{execution_id}/context-usage` returns the latest context health and compaction records:

```json
{
  "execution_id": "execution-1",
  "workflow_id": "workflow-1",
  "source": "execution.metadata.runtime_governance",
  "latest_context_health": {
    "status": "warning",
    "estimated_total_context_tokens": 9000,
    "context_window": 128000
  },
  "latest_compaction": {
    "compacted": true,
    "reason": "context_health_threshold",
    "estimated_tokens_saved": 1200,
    "memory_id": "memory-compact"
  },
  "compaction_records": [],
  "protected_context": {
    "retained": true,
    "protected_message_count": 3,
    "protected_message_roles": ["system", "user", "tool"],
    "protected_message_reasons": {
      "0": "system_message",
      "1": "user_message",
      "3": "pending_human_decision"
    }
  }
}
```

The execution event stream is still the audit source of truth. Frontend timelines should render these governance events
when present:

- `context.health.recorded`
- `token.usage.recorded`
- `token.budget.warning`
- `token.budget.exceeded`
- `context.compaction.started`
- `context.compaction.completed`
- `context.compaction.failed`
- `supervisor.steering.requested`
- `supervisor.steering.applied`

Use `/executions/{execution_id}/events` with `event_type` filters when rendering governance-specific timelines. This
keeps token, context, compaction, and steering views from downloading unrelated execution events.

The runtime stream maps governance events into `LOG_RECEIVED` and `TASK_PROGRESS` entries, so existing execution screens
can show them without introducing new stream event enum values. For first release, do not add dedicated token/context
runtime stream event types unless a concrete UI requires lower-latency live updates than filtered event replay provides.
Suggested UI treatment:

- show token totals and estimated cost from `state.runtime_governance.token_usage.total`
- show per-agent, per-task, and per-model totals from `state.runtime_governance.token_usage.by_agent`,
  `by_task`, and `by_model`
- show a context status badge from `state.runtime_governance.context_health.last.status`
- show protected-context retention from `/context-usage.protected_context`; this is an indicator/count and role/reason
  breakdown, not raw prompt text
- show warning styling and history rows for token budget warning/exceeded events; native `pause_execution` and
  `fail_execution` actions are reflected by the normal execution status, while `compact_context` also emits compaction
  events
- show compaction events in the execution timeline, but do not display raw prompts by default
- show pending main-agent supervision requests from `state.runtime_governance.supervision.pending_requests`

Budget events are enforced for native executions according to policy: `warn_only`, `compact_context`,
`pause_execution`, and `fail_execution`. Supervisor steering remains policy-gated: the backend emits
`supervisor.steering.requested`, and only applies steering actions that the workflow explicitly allows or the operator
approves.

Workflow runtime governance controls are exposed from `GET /workflows/{workflow_id}/runtime-governance` and can be
updated with `PATCH /workflows/{workflow_id}/runtime-governance`. `GET /workflows/{workflow_id}` also includes the same
operator payload as `runtime_governance`. Relevant editable controls include:

- `tokenBudget.runTotalTokens`
- `tokenBudget.workflowTotalTokens`
- `tokenBudget.agentTotalTokens`
- `tokenBudget.warnRatio`
- `tokenBudget.hardRatio`
- `tokenBudget.action`: `warn_only`, `compact_context`, `pause_execution`, or `fail_execution`
- `contextCompaction.enabled`
- `contextCompaction.persistContextPack`
- `contextCompaction.preserveRecentMessages`

`GET /workflows/{workflow_id}` also returns response-only operator payloads such as `monitoring` and
`runtime_governance`. Those are useful for UI inspection, but they are not accepted by
`POST /workflows/validate`. Frontends or scripts that validate a stored workflow definition should strip those fields
before sending the payload back to the validator.
- `contextCompaction.oversizedMessageTokens`
- `contextCompaction.minEstimatedTokensSaved`
- `contextCompaction.maxSummaryChars`

Example patch:

```json
{
  "tokenBudget": {
    "runTotalTokens": 100000,
    "warnRatio": 0.8,
    "hardRatio": 1.0,
    "action": "compact_context"
  },
  "contextCompaction": {
    "enabled": true,
    "persistContextPack": false,
    "preserveRecentMessages": 3,
    "maxSummaryChars": 5000
  }
}
```

This backend repository exposes the canonical fields and API contracts. The frontend implementation lives in the
separate `open-agency-fe` repository and renders these as workflow governance fields rather than requiring raw metadata JSON
editing. Unknown fields in the runtime-governance PATCH payload are rejected with FastAPI validation errors, so form
controls should send only the documented keys.

Observability metrics now include governance summaries for dashboards that do not need to load a single execution
detail snapshot. `GET /observability/agents/{agent_id}/metrics` returns token/cost totals plus `context_health`,
`budget`, and `compaction` summaries for that agent. `GET /observability/workflows/{workflow_id}/metrics` returns the
same governance summary family at workflow scope and falls back from event `workflow_id` to the parent execution
workflow id when needed. `GET /observability/models/usage` returns model usage buckets and echoes the active filters:

```http
GET /observability/models/usage?workflow_id=workflow-1&agent_id=agent-1&provider=openai&model=gpt-4.1-mini
```

Persistence for these summaries is intentionally JSON/event based: current execution snapshots live under
`Execution.metadata.runtime_governance` and are backed by `executions.metadata_json` in SQL stores. Immutable evidence
lives in `ExecutionEvent.payload` and `ExecutionEvent.metrics`. Do not add frontend assumptions about usage aggregate
tables.

Workflow monitoring controls are exposed from `GET /workflows/{workflow_id}/monitoring` and can be updated with
`PATCH /workflows/{workflow_id}/monitoring`. Relevant governance controls include:

- `supervise_token_usage`
- `supervise_context_health`
- `supervise_subagents`
- `supervise_tool_failures`
- `excluded_subagent_ids`
- `excluded_task_ids`
- `allowed_steering_actions`
- `auto_apply_steering_actions`
- `route_steering_requests_to_approval`
- `delegate_hitl_to_main_agent`

The monitoring response separates effective policy from explicit workflow metadata:

- `controls`: effective values the monitor will use.
- `explicit_controls`: tri-state operator settings, where `true` and `false` mean explicitly configured and `null`
  means the value comes from backend policy defaults.
- `control_sources`: per-control source labels, currently `explicit` or `policy_default`.

For example, `controls.allow_evaluation_agent_review=true` with
`explicit_controls.allow_evaluation_agent_review=null` means the backend may still request Evaluation-agent review for
failed, stale, strict, sensitive, or recently changed workflows. Set
`allow_evaluation_agent_review=false` explicitly when the workflow must suppress those reviews.

Use `excluded_subagent_ids` or `excluded_task_ids` when a workflow should stay monitored overall but a specific
sub-agent or task should not trigger main-agent supervision findings.

`auto_apply_steering_actions` is intentionally opt-in and should only include operational actions that the workflow is
allowed to apply without another approval step, such as `repair_stale_execution`. If omitted, the monitor only records
advisory `supervisor.steering.requested` events.

Set `route_steering_requests_to_approval` to `true` together with `approval_conversation_id` when risky supervisor
steering should create an approval request instead of remaining only in the execution trace. Approving the request emits
`supervisor.steering.applied` and marks the pending request as applied. Rejected requests are marked rejected without
applying a steering action. Approved requests can now produce one of these result statuses:

- `workflow_updated`: a mutable workflow was revised with deterministic steering guidance and active executions were
  replacement-eligible through the workflow revision replacement path.
- `recorded_guidance`: the workflow was not mutable, so the approval was persisted as execution-level steering guidance.
- `human_review_recorded`: a human-review request was captured in execution governance metadata.
- `execution_paused`, `execution_resumed`, `execution_cancelled`, or `stale_execution_repaired`: an approved operational
  action was applied through the execution control plane.

Supervisor steering approval requests include `proposed_payload.operator_parameter_schema`. Workflow monitoring UI
should render those fields and send approved values as `steering_parameters` to
`POST /conversations/approval-requests/{approval_request_id}/approve`. Supported parameter keys include
`target_agent_id`, `target_task_id`, `instructions`, `max_iterations`, `remove_tool_ids`, and `review_note`. The backend
validates submitted keys against the steering action and verifies selected task, agent, and tool IDs against the target
workflow before resolving the approval.

Workflow Builder exposes these controls as part of workflow governance, not only as raw metadata editing. The Workflow
Graph also reflects the policy on nodes: monitored, excluded, inherited from workflow defaults, and any pending
supervisor steering request. Excluded nodes should remain visible in execution evidence; the exclusion only suppresses
main-agent supervision findings and steering requests for matching sub-agent events.

Monitor findings are persisted as `monitor.finding.created` execution events. The monitor writes a stable
`metadata.dedupe_key` based on workflow id, execution id, category, status, and source event id or source event ids, and
suppresses duplicate writes when that key already exists in persisted history. Terminal findings, including
`completed_execution`, are also de-duplicated; UI history should treat repeated scans of the same completed or failed
execution as current status, not as new evidence.

`GET /executions/{execution_id}` also includes sub-agent callback state at `state.runtime_callbacks` when internal
sub-agent callbacks have reported progress. Checkpoints may include structured status fields:

```json
{
  "state": {
    "runtime_callbacks": {
      "checkpoints": {
        "task-1": {
          "agent_id": "agent-1",
          "step_id": "task-1",
          "status": "running",
          "subagent_status": "blocked",
          "current_task": "Validate rollout plan",
          "blocker": "Missing production window",
          "confidence": 0.4,
          "progress_percent": 50,
          "token_usage": {"total_tokens": 140},
          "context_health": {"status": "normal"}
        }
      }
    }
  }
}
```

Frontend execution timelines should render `subagent.progress.updated`, `subagent.step.completed`,
`subagent.step.failed`, `subagent.needs_input`, and `subagent.needs_approval` as agent/task progress events. Blockers,
clarification-needed states, and approval/input requests should use warning styling; failed sub-agent steps should use
error styling.

## Event Streaming Flow

The internal source of truth is the execution event log.

Frontend-facing behavior is built from:

- `GET /executions/{execution_id}/events`
- observability endpoints that aggregate the event stream
- `GET /executions/{execution_id}/stream` for live updates

If a screen needs timeline reconstruction, event-level details should be preferred over derived summary fields.

## Approval Flow

Approval-aware tools and approval-gated supervisor steering expose security metadata in their definitions or approval
payloads. The expected UI flow is:

1. Frontend starts an execution that includes approval-gated tools.
2. Runtime creates an approval request when a gated tool is about to execute.
3. Frontend reads approval state from execution and event data.
4. User approves or rejects.
5. Runtime resumes and records the decision in execution events and approval request state.

Workflow monitoring controls include `delegate_hitl_to_main_agent`. When enabled, the main-agent monitor can treat
HITL review steering as delegated to the main agent instead of creating a human conversation approval. The run detail UI
should display this as an execution governance indicator so operators can see whether HITL is human-held or delegated.
The native runtime also uses this same opt-in policy for low-risk approval-gated tools. Delegated approvals are visible
on `approval.granted` events through `payload.decision_metadata.mode = "delegated"` and
`payload.decision_metadata.delegate = "main_agent"`. High-risk approval requests, including local privileged execution,
filesystem, browser, shell, network, MCP, credential, dangerous, or mutation labels, remain manual and should continue to
render approve/reject controls. Run detail reconstructs native approval activity from `approval.requested`,
`approval.granted`, `approval.rejected`, `execution.metadata.pending_approval`, and persisted rows from
`GET /executions/{execution_id}/approvals`; conversation approvals remain a separate section because they use the
conversation approval API.

`GET /executions/{execution_id}/approvals` returns the durable native approval rows for an execution:

```json
{
  "items": [
    {
      "id": "execution-1:tool-1",
      "execution_id": "execution-1",
      "event_id": "event-approval-requested",
      "tool_id": "tool-1",
      "status": "approved",
      "request_payload": {
        "arguments": {"text": "approve me"},
        "approval_metadata": {"risk_labels": ["requires_approval"]}
      },
      "response_payload": {
        "granted": true,
        "reason": "Main-agent delegated HITL approval.",
        "metadata": {"mode": "delegated", "delegate": "main_agent"}
      },
      "requested_at": "2026-05-25T06:00:00Z",
      "responded_at": "2026-05-25T06:00:01Z",
      "responded_by": "main_agent"
    }
  ]
}
```

Use these rows when available for `responded_by`, request/response history, and stable approval status. Keep the event
stream fallback because older runs and non-SQL stores may only have `approval.*` events or pending execution metadata.

Supervisor steering approvals should render the `operator_parameter_schema` fields and a pre-approval preview. The
preview should show the recommended action, expected effect, selected target task/agent/tool values, and severity or
confidence when present.

For interactive CrewAI runs, the canonical HITL endpoints are:

- `GET /executions/{execution_id}/hitl/stream`
- `POST /executions/{execution_id}/hitl/reply`

## LLM OAuth Flow

For OAuth-capable model providers, the FE should use the model-provider routes as the canonical auth API.

Provider-side state:

- OAuth tokens live in `model_provider.config`
- multi-account OAuth state lives in `model_provider.config.auth_profiles`
- the default account lives in `model_provider.config.default_oauth_profile_id`
- a model profile can optionally point at a specific account using `model_profile.parameters.oauth_profile_id`

Frontend behavior for the `LLM Models` page:

1. Load provider and profile catalogs.
2. Render OAuth status from the provider record, not from copied profile token fields.
3. When the user starts ChatGPT OAuth, call `POST /model-providers/{provider_id}/authorize`.
4. Preserve the returned `pkce_verifier`, `state`, `redirect_uri`, and `auth_profile_id` in UI state until
   completion.
5. Complete the flow with `POST /model-providers/{provider_id}/callback-complete`.
6. If loopback capture fails, allow the user to paste the full redirect URL and send it as `redirect_url`.
7. For headless flow, use `device-authorize` and `device-complete`.
8. Refresh the provider record after completion and re-render connection-level OAuth status.

Expected request shapes:

```json
POST /model-providers/{provider_id}/authorize
{
  "auth_profile_id": "default"
}
```

```json
POST /model-providers/{provider_id}/callback-complete
{
  "code": "...",
  "pkce_verifier": "...",
  "state": "...",
  "auth_profile_id": "default"
}
```

```json
POST /model-providers/{provider_id}/callback-complete
{
  "redirect_url": "http://127.0.0.1:1455/auth/callback?code=...&state=...",
  "auth_profile_id": "default"
}
```

```json
POST /model-providers/{provider_id}/device-authorize
{
  "auth_profile_id": "default"
}
```

```json
POST /model-providers/{provider_id}/device-complete
{
  "device_code": "...",
  "auth_profile_id": "default"
}
```

UI guidance:

- show one provider connection with zero or more OAuth profiles/accounts beneath it
- show `account_id` when returned by the backend
- allow setting which OAuth profile is the provider default
- let a model preset opt into a specific OAuth profile only when needed
- avoid editing raw token fields in the FE

## Conversation Model Auth Recovery

Main-agent chat is LLM-first for plain user text. The frontend should submit normal chat messages to
`POST /conversations/{conversation_id}/messages` and let the backend resolve the active main-agent model profile. The
configured model can be Codex, Ollama, or any registered provider.

Browser chat should send `response_mode: "async"`. The backend returns `200 OK` after persisting the user message and
includes a `stream_url`; the assistant response then arrives through `GET /conversations/{conversation_id}/stream`.
Avoid long synchronous browser/proxy waits for LLM calls because model planning and tool use can exceed frontend reverse
proxy timeouts. Treat the stream as the live path, but also backfill from `GET /conversations/{conversation_id}/messages`
after async sends until a message appears after the submitted user message. This keeps the UI correct when a local/dev
SSE connection misses an in-memory event during backend reloads or process changes.

When the active model cannot be used because provider auth is missing, expired, or lacks required scopes, the backend
should still return `200 OK` with an `assistant_text` message. The frontend should inspect:

```json
{
  "assistant_message": {
    "metadata": {
      "model_auth": {
        "auth_required": true,
        "reauthorization_required": true,
        "auth_action": "device_authorize",
        "auth_endpoint": "/model-providers/openai-codex/device-authorize",
        "provider_id": "openai-codex"
      }
    }
  }
}
```

If `reauthorization_required` is true, render a model re-auth action and call `auth_endpoint` using the provider's normal
OAuth/device-flow request shape. Do not special-case all assistant failures as Codex failures; the backend supplies
`provider_id`, `auth_action`, and `auth_endpoint` for the active model.

## Retired Runtime Namespaces

These route families are not frontend targets:

- `/api/crew/*`
- `/api/history/*`
- `/api/artifacts/*`
- `/api/hitl/*`

These legacy route families are retired compatibility references only. They remain useful only as historical migration
references, not as active frontend targets. Runtime adapter-specific behavior, including CrewAI, should be reached
through canonical workflow and execution routes.
