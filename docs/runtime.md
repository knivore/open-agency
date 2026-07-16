# Runtime

## Overview

The runtime layer owns workflow execution behavior. It isolates framework-specific concerns behind runtime adapters and
keeps the canonical execution model in `app/domain`.

The active runtime areas are:

- `app/runtime/native`
- `app/runtime/adapters`
- `app/runtime/control_plane.py`
- `app/runtime/worker.py`
- `app/runtime/reconcile.py`
- `app/runtime/containers.py`

The preferred local mode is to run the backend and main-agent process on the host and keep execution workers in Docker:

```bash
./run.sh start
```

This keeps main-agent chat on the host Codex CLI and host `~/.codex`, while native workflow and tool execution can
remain isolated through worker containers when `EXECUTION_ISOLATION_ENABLED=true`.

Relevant local mode settings:

- `AGENCY_BACKEND_RUN_MODE=host` keeps the backend and main-agent process on the host while isolated workers stay in Docker
- `EXECUTION_ISOLATION_ENABLED=true` enables Docker-backed isolated execution for workflow and tool runs
- `EXECUTION_WAIT_POLL_INTERVAL_SECONDS=1` controls how frequently the reconciler checks durable waits for due wakes or
  expired deadlines

Runtime event and notification features:

- [Outbound Runtime Webhooks](./runtime/outbound-webhooks.md)
- [Internal Sub-Agent Callbacks](./runtime/subagent-callbacks.md)

Outbound runtime webhooks are backend service-side notifications from Agency to registered external targets. Internal
sub-agent callbacks are service calls that write `subagent.*` execution events and checkpoint metadata for supervisor or
main-agent continuation. Neither feature exposes public third-party runtime webhook triggers.

Workflow executions can also be linked to a durable goal. Launch callers should pass `goal_id` in the execution
creation payload, trigger metadata, or runtime input when the run is an attempt under a long-running objective. The
runtime keeps execution events as the source of truth for activity; the goal layer uses those events, artifacts,
approval records, and evidence/evaluation payloads to decide whether the objective is progressing or complete.

## Backend Process And Container Startup

Start the FastAPI backend directly:

```bash
make dev
```

Equivalent host command:

```bash
SSL_CERT_FILE=certs/local_cloudflare.cert ./.venv/bin/python -m uvicorn app:app --reload
```

Windows PowerShell equivalent:

```powershell
$env:SSL_CERT_FILE = "certs/local_cloudflare.cert"
.\.venv\Scripts\python.exe -m uvicorn app:app --reload
```

If you are running the backend directly on Windows instead of through the launcher, start Postgres and Redis first:

```powershell
docker compose up -d postgres redis
.\.venv\Scripts\python.exe -m alembic upgrade head
$env:SSL_CERT_FILE = "certs/local_cloudflare.cert"
.\.venv\Scripts\python.exe -m uvicorn app:app --reload
```

Once the server is running, the built-in API docs are available at:

- `http://localhost:8000/docs`
- `http://localhost:8000/redoc`
- `http://localhost:8000/openapi.json`

The canonical app factory is `app.api.main:create_app()`. The root [`app.py`](../app.py) file is intentionally a thin
entrypoint.

Container definitions are grouped under:

- [`docker/backend/Dockerfile`](../docker/backend/Dockerfile)
- [`docker/postgres/Dockerfile`](../docker/postgres/Dockerfile)
- [`docker/postgres/initdb/001-extensions.sql`](../docker/postgres/initdb/001-extensions.sql)
- [`docker/redis/Dockerfile`](../docker/redis/Dockerfile)
- [`docker/redis/redis.conf`](../docker/redis/redis.conf)
- [`docker/langfuse/README.md`](../docker/langfuse/README.md)

The backend image is built from [`docker/backend/Dockerfile`](../docker/backend/Dockerfile). Backend container startup
waits for Postgres and Redis, runs `alembic upgrade head`, and then starts `uvicorn app:app`.

This is why the repo has both host launch scripts and a container `entrypoint.sh`:

- `run.sh` and `run-windows.sh` manage the host dev stack
- `docker/backend/entrypoint.sh` owns self-contained backend-container startup

That separation lets the backend container restart correctly even when the host scripts are not involved.

## Observability Stack

The full Docker Compose stack includes self-hosted Langfuse.

Useful local endpoints:

- backend API: `http://localhost:8000`
- Langfuse UI/API: `http://localhost:3001`
- Langfuse MinIO S3 endpoint: `http://localhost:9090`
- Langfuse MinIO console: `http://localhost:9091`

The backend container is wired with:

- `LANGFUSE_BASE_URL=http://langfuse-web:3001`
- `LANGFUSE_HOST=http://langfuse-web:3001` for older SDK compatibility
- `LANGFUSE_PUBLIC_KEY`
- `LANGFUSE_SECRET_KEY`
- `OBSERVABILITY_EXPORTERS`
- `OBSERVABILITY_REDACT_SECRETS=true`
- `OBSERVABILITY_JSONL_PATH=logs/observability.jsonl`

By default, local backend runs still use `OBSERVABILITY_EXPORTERS=jsonl`. To include Langfuse in local exporter
selection:

```env
OBSERVABILITY_EXPORTERS=jsonl,langfuse
LANGFUSE_BASE_URL=http://localhost:3001
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

JSONL remains the append-only local audit trail. The Langfuse exporter maps LLM responses to `generation`
observations, tool calls to `tool` observations, and approvals/actions/runtime events to spans with execution, agent,
tool-call, sequence, risk-label, and redaction metadata.

## Native Runtime

The native runtime is the preferred execution path for the app-centric architecture.

Product default:

- new workflows should default to `native`
- alternate adapters such as `crewai` should be opt-in per workflow or per execution request
- future adapters should follow the same model

Responsibilities:

- load workflow, agent, and tool definitions through repositories and registries
- create execution records
- append canonical execution events
- persist artifacts, approvals, and tool invocations
- enforce tool security checks and approval boundaries
- inject opt-in shared durable memory from `memory_records` into task prompts
- expose execution status updates through the control plane

The internal source of truth is the execution event stream, not framework-specific logs.

For isolated execution today, the native runtime is also the only adapter that runs inside the managed worker container.

### Runtime Timeout Policy

Before an execution starts, Agency resolves timeout configuration into `execution.metadata.runtime_policy`. The same
resolved policy is used by the control plane, isolated worker environment, and main-agent workflow monitor so worker hard
timeouts, stale-run detection, Codex CLI timeouts, and LLM request timeouts do not drift apart.

Default values come from:

- `AGENT_RUN_TIMEOUT_SECONDS`
- `AGENT_ACTIVITY_IDLE_TIMEOUT_SECONDS`
- `CODEX_CLI_TIMEOUT_SECONDS`
- `LLM_REQUEST_TIMEOUT_SECONDS`

Workflow, task, agent, trigger, or input metadata can override those defaults with `runtime_policy`; existing
`timeout_policy` metadata is still accepted for compatibility. Isolated worker startup resolves the longest task or
agent timeout in the workflow before creating the container, which lets long-running agents extend the actual worker
lifetime before any task activity event has been emitted.

### Durable Wait Lifecycle

Native executions can persist one pending `execution_waits` record when they are suspended at a node checkpoint. Wait
kinds map to distinct execution statuses:

- `input` -> `waiting_for_input`
- `approval` -> `waiting_for_approval`
- `event` -> `waiting_for_event`
- `sleep` -> `sleeping`

The wait record owns its idempotency key, checkpoint, request payload, policy, correlation key, wake/deadline times, and
resolution. The execution record keeps an `active_wait` summary for fast operator reads, and `execution.waiting` plus
`execution.woken` events preserve the ordered audit trail. Only one pending wait is allowed per execution.

Lifecycle endpoints:

- `POST /executions/{execution_id}/waits`
- `GET /executions/{execution_id}/waits`
- `GET /executions/{execution_id}/waits/{wait_id}`
- `POST /executions/{execution_id}/waits/{wait_id}/resolve`
- `POST /execution-waits/events/{correlation_key}`

Resolution is an atomic single claim. Repeating the same resolution key is an idempotent no-op; a competing key is a
conflict. Generic execution start/resume commands reject unresolved waits so they cannot bypass the wake claim. Due
sleep waits and expired deadlines are reconciled at backend startup and on the runtime reconciler cadence. Native input,
event, and sleep waits resume the same execution from persisted node outputs, including after process-local engine state
has been lost.

Tool approval requests and their linked waits are also persisted. Before a gated tool asks for approval, the native
agent executor checkpoints its message transcript, model iteration, remaining tool calls, and pending call index. The
approval suspension then unwinds the worker or isolated container. Approve and reject decisions resolve the durable wait,
emit `execution.woken`, and queue a fresh native worker; that worker consumes the persisted decision and resumes at the
pending call without replaying prior tool calls from the same model response. Low-risk main-agent delegated approvals
continue inline because they do not incur a human wait.

### Persistent Monitor Cycles

A persistent monitor is an opt-in native workflow that repeatedly runs its complete graph under one execution ID. It is
different from a scheduled workflow, which creates finite execution attempts, and from an `always_on` workflow without
a cycle policy, which only receives relaxed timeout semantics.

Configure the workflow definition metadata as follows:

```json
{
  "execution_lifecycle": {
    "persistent_cycle": {
      "enabled": true,
      "interval_seconds": 60,
      "jitter_ratio": 0.1,
      "failure_backoff_multiplier": 2,
      "max_interval_seconds": 3600,
      "max_consecutive_failures": 5,
      "max_cycles": null,
      "max_no_progress_cycles": null,
      "deadline_at": null,
      "history_limit": 20
    }
  }
}
```

Enabling `persistent_cycle` implies the `always_on` run mode. A successful cycle emits
`execution.cycle.completed`, clears the completed-node checkpoint, preserves the prior result under
`output_payload.last_cycle_output`, and creates a durable `sleep` wait for the next cycle. Timer reconciliation wakes
that wait and queues the same execution ID. Cycle state, progress signatures, recent outcomes, retry count, and next
wake time are stored under `execution.metadata.persistent_cycle`.

Failed cycles emit `execution.cycle.failed` and retry after bounded exponential backoff. `jitter_ratio` applies stable
per-execution jitter so multiple monitors do not wake together. `max_consecutive_failures` and the optional
`max_no_progress_cycles` repeated-output guard pause the execution and emit `execution.cycle.guard_triggered` instead of
spinning indefinitely. `max_cycles` and `deadline_at` are optional terminal policies; when both are omitted, the monitor
continues until an operator pauses or cancels it.

Pausing a sleeping monitor cancels its pending timer and leaves the execution paused; resuming runs the next cycle
immediately. Cancelling closes any pending wait and terminally cancels the execution. In isolated mode, a worker parked
on a durable wait exits with the suspended-worker code, and reconciliation removes that finished container without
changing the execution's wait status. The next wake creates a fresh worker and resumes from durable state.

In the frontend, edit a workflow and open **Configuration & governance** to choose **Persistent monitor** under
**Execution mode**. The editor exposes the interval, failure backoff, maximum interval, consecutive-failure guard,
optional repeated-result guard, and optional cycle limit. Run detail displays durable wait state, current and next cycle,
next wake time, failure/no-progress signals, and the valid operator actions for the execution state. Approval waits show
the saved checkpoint, worker-release state, and direct approve/reject-and-resume controls. A sleeping monitor can be
paused or cancelled; a guard-paused monitor can be resumed or cancelled.

Shared memory is retrieved before each native task when workflow metadata has `shared_memory.enabled=true` or the assigned
agent has `memory.enabled=true` with a non-`execution` scope. Retrieval uses the canonical memory service and can draw
from relevant workflow, workspace, user, global, and agent-attributed records. Operators can update the setting through
`PATCH /workflows/{workflow_id}/shared-memory`.

### Agency Graph Runtime Context

The native runtime can retrieve read-only Agency Graph context at runtime trigger points when graph context is enabled
for the assigned agent and the global graph-context feature flags allow it. The graph is not a replacement for durable
memory: durable memory supplies reusable semantic facts and context packs, while Agency Graph context supplies bounded
relationship, lineage, prior-attempt, failure, decision, and next-action context around the current run.

Runtime graph context can be retrieved before sub-agent start, before coding-agent resume work, after execution failure,
after context compaction, and before proposal tools. Retrieved entries are appended to native execution state under
`graph_context_entries`, capped to the recent working set, and surfaced to the next task prompt as a synthetic
`runtime_graph_context` message. Each successful retrieval can also create or update an in-memory graph working set so a
later tool call can summarize, curate, or persist that working set as a `memory_type=context_pack` memory.

Runtime graph retrieval is intentionally bounded:

- `AGENCY_GRAPH_CONTEXT_TOOLS_ENABLED` controls graph tool discovery and setup defaults.
- `GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED` controls automatic runtime retrieval.
- `GRAPH_CONTEXT_SUBAGENT_STEERING_ENABLED` controls sub-agent steering retrieval.
- `GRAPH_CONTEXT_CODING_AGENT_RESUME_ENABLED` controls coding-agent resume retrieval.
- `GRAPH_CONTEXT_LOOP_GUARD_ENABLED` prevents repeated retrieval when no runtime progress has happened since the last
  similar graph context request.
- graph queries also honor `AGENCY_GRAPH_CONTEXT_QUERY_TIMEOUT_SECONDS`,
  `AGENCY_GRAPH_CONTEXT_RATE_LIMIT_WINDOW_SECONDS`, and `AGENCY_GRAPH_CONTEXT_RATE_LIMIT_MAX_UNITS`.

Graph context emits runtime events and runtime-operation counters for calls, failures, output sizes, context-pack
creation, and auto-retrieval injections. These counters are intended for diagnosing graph-context behavior without
dumping raw graph data into prompts or logs.

### Runtime Governance

Native executions now record runtime-governance telemetry around each model call.

Before every native LLM request, the runtime estimates context health from the assembled `ModelMessage` list, the model
profile context window, and reserved completion tokens. It emits `context.health.recorded` and includes context fields on
the following `llm.request.created` metrics:

- `estimated_prompt_tokens`
- `reserved_completion_tokens`
- `estimated_total_context_tokens`
- `context_window`
- `context_usage_ratio`
- `context_status`

After every native LLM response, provider usage is normalized into canonical token fields and emitted as
`token.usage.recorded`. The runtime also keeps backward-compatible `input_tokens` and `output_tokens` metrics for older
observability consumers. Canonical fields are:

- `prompt_tokens`
- `completion_tokens`
- `total_tokens`
- `estimated_cost`
- `token_usage_estimated`

Token cost is estimated only from model-profile parameters, such as `input_token_cost_per_1m`,
`output_token_cost_per_1m`, `cached_input_token_cost_per_1m`, and `currency`. The backend does not hard-code public model
pricing.

When a model profile has fallback enabled, the runtime may switch from the primary model to a backup model for allowed
retryable failures. Successful switches emit `model.fallback.used` before `llm.response.created`; exhausted fallback
chains emit `model.fallback.failed` before the model error propagates. The canonical event payloads include:

- `model_request_id`
- `model_profile_id`
- `primary_provider`
- `primary_model`
- `fallback_provider` and `fallback_model` for successful switches
- `fallback_index`
- `attempts` with provider, model, error type, retryable flag, and category
- `error` for exhausted fallback chains

Successful fallback metadata is also copied into normalized token usage at
`usage.provider_usage.model_fallback`, so run usage snapshots and model usage aggregation can show fallback rate and
primary models that commonly fall back.

Executions expose the latest aggregate snapshot under `execution.metadata.runtime_governance`, which is also returned by
`GET /executions/{execution_id}` under `state.runtime_governance`. The snapshot may include:

- `token_usage.total`
- `token_usage.by_agent`
- `token_usage.by_task`
- `token_usage.by_model`
- `context_health.last`
- `context_compaction.last`
- `budget_warnings_emitted`

For SQL-backed deployments this snapshot is persisted on `executions.metadata_json`, not only inside the execution
trigger payload. `SQLExecutionStore.save_execution()` writes `metadata_json` on both create and update paths, so context
health and token usage snapshots survive later status, output, error, heartbeat, and terminal completion saves.

Narrow read APIs are available when a UI or API client does not need the full execution detail payload:

- `GET /executions/{execution_id}/usage`
- `GET /executions/{execution_id}/context-usage`
- `GET /executions/{execution_id}/approvals`

The runtime governance source-of-truth model is:

- detailed immutable history in `ExecutionEvent.payload`, `ExecutionEvent.metrics`, and `ExecutionEvent.metadata`
- current read snapshot in `Execution.metadata.runtime_governance`, backed by `executions.metadata_json` for SQL stores
- compacted summaries in `memory_records` with `memory_type=context_pack`
- persisted native approval rows in `approval_requests`

`GET /executions/{execution_id}/usage` returns a token-focused projection. It intentionally omits the internal
`processed_event_ids` idempotency list. Run Detail uses this response for run-level summary cards plus per-agent,
per-task, per-model, and budget-warning breakdown rows.

```json
{
  "execution_id": "execution-1",
  "workflow_id": "workflow-1",
  "source": "execution.metadata.runtime_governance",
  "token_usage": {
    "total": {
      "prompt_tokens": 1200,
      "completion_tokens": 300,
      "total_tokens": 1500,
      "estimated_cost": 0.0123,
      "currency": "USD"
    },
    "by_agent": {"agent-1": {"total_tokens": 1500}},
    "by_task": {"task-1": {"total_tokens": 1500}},
    "by_model": {"openai:gpt-4.1-mini": {"total_tokens": 1500}},
    "fallback_count": 1,
    "model_fallbacks": [
      {
        "primary_provider": "openai",
        "primary_model": "gpt-4.1",
        "fallback_provider": "openai",
        "fallback_model": "gpt-4.1-mini",
        "fallback_index": 1
      }
    ]
  },
  "budget_warnings": [],
  "updated_at": "2026-05-25T06:00:00Z"
}
```

`GET /executions/{execution_id}/context-usage` returns latest context pressure and compaction state:

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

`GET /executions/{execution_id}/approvals` returns persisted native approval rows. Use this endpoint for durable
request/response payload history and `responded_by`; use `approval.*` execution events for timeline order and audit
details.

Token budgets can be supplied by convention through `runtime_governance.token_budget` or `token_budget` on workflow,
agent, task, execution input, or execution trigger payloads. When no explicit budget is supplied, the resolver can use
global defaults from `AGENT_RUN_TOTAL_TOKEN_BUDGET`, `AGENT_TOKEN_BUDGET_WARN_RATIO`,
`AGENT_TOKEN_BUDGET_HARD_RATIO`, and `AGENT_TOKEN_BUDGET_ACTION`. Explicit workflow, agent, task, input, and trigger
settings override global defaults. The runtime emits `token.budget.warning` or `token.budget.exceeded` when configured
thresholds are crossed. Native execution currently enforces hard-limit actions:

- `warn_only`: emit budget events and continue.
- `compact_context`: best-effort compact prompt context before the next model iteration when a compactor is available.
- `pause_execution`: pause the execution through the native execution state path.
- `fail_execution`: fail the execution after recording the exceeded event.

When native context health reaches `critical` or `overflow`, or when a model provider raises a recognizable
context-length error, the runtime can compact older or oversized assistant/tool context before retrying the model
request. Compaction emits:

- `context.compaction.started`
- `context.compaction.completed`
- `context.compaction.failed`

The deterministic compactor preserves system/task instructions and latest user input as raw prompt context, explicitly
retains prompt messages marked as pending approvals, pending human input, unresolved tool errors, or
`protected_from_compaction`, summarizes older assistant/tool context into a synthetic system message, leaves execution
events unchanged for audit, and stores compaction metadata under `runtime_governance.context_compaction`. The synthetic
summary includes a `Protected Context Retained` section and the compaction record includes protected-message counts,
roles, and reasons for operator inspection. Runtime compaction always updates execution metadata and events; persisting
the compacted summary as workflow-scoped `context_pack` memory is opt-in via
`workflow.metadata.runtime_governance.context_compaction.persist_context_pack=true`. Deployments can change the fallback
default with `AGENT_CONTEXT_COMPACTION_PERSIST_CONTEXT_PACK_DEFAULT`, which defaults to `false`. Runtime context packs
carry `agent_id`, `task_id`, `execution_id`, `source_model_request_id`, compaction status, compaction reason, estimated
token savings, and the audit sequence range covered by the compacted source context in metadata. The synthetic prompt
block also starts with `Runtime Context Compaction State`, including `context_compacted=true`, `compaction_reason`,
`context_pack_memory_id`, `source_model_request_id`, and `estimated_tokens_saved`, so the agent can inspect that
compaction happened before continuing.

During native execution, `NativeExecutionState.context_compaction` mirrors the latest in-run compaction records and
`NativeExecutionState.compacted_context_packs` lists persisted context-pack ids when persistence is enabled. This is a
runtime convenience for steering and follow-up logic; execution events and `Execution.metadata.runtime_governance` remain
the durable audit/read model.

Conversation direct-reply model calls and conversation compaction LLM calls also use the same governance services. Events
are persisted on the conversation audit execution id `conversation-audit-{conversation_id}` with
`workflow_id=conversation-main-agent` and `runtime_adapter_id=conversation`. Each model call emits:

- `context.health.recorded`
- `llm.request.created`
- `llm.response.created`
- `token.usage.recorded`

The conversation audit execution also receives `runtime_governance.context_health` and
`runtime_governance.token_usage` snapshots, so existing execution usage/context endpoints can inspect that audit run.
Conversation events include `call_kind=direct_reply` or `call_kind=conversation_compaction` so operator UIs can separate
chat responses from compaction calls.

CrewAI bridge calls through `AgencyModelClientLLM` also use the same governance services when the CrewAI adapter has an
Agency execution store. Events are emitted with `call_kind=crewai_bridge`, `runtime_adapter_id=crewai`, and best-effort
agent/task attribution from the mapped CrewAI agent and task. Framework-native CrewAI log replay can still emit
lower-fidelity `llm.*` events with `metadata.source=crewai_log`, so consumers should prefer `crewai_llm_bridge` events
when both are present.

Developer extension rules:

1. Add or reuse a typed domain model in `app/domain/runtime_governance.py`.
2. Emit a canonical `ExecutionEvent` with enough payload or metrics to audit the decision.
3. Update `Execution.metadata.runtime_governance` only with the current read snapshot; do not rely on event replay to
   reconstruct the operator-facing current state.
4. Add a narrow read API only when UI/API consumers need a stable projection.
5. Add execution-service log text and runtime-stream mapping if operators need live visibility.
6. Add focused tests for the recorder, route, persistence update path, and UI rendering path.

## Isolated Runtime Operations

The isolated execution runtime supports:

- one Docker container per execution for isolated runs
- runtime revision tracking and replacement
- worker-owned execution inside the container
- immediate container-exit reconciliation
- scheduled runtime reconciliation when `RUNTIME_RECONCILER_ENABLED=true`
- TTL-based exited-container cleanup and retention-based managed-image cleanup
- runtime metrics and recent runtime actions
- execution/container log visibility through the API

Operator endpoints:

- `GET /executions/runtime/metrics`
- `GET /executions/runtime/containers`
- `GET /executions/runtime/containers/{container_id}/logs`
- `POST /executions/runtime/reconcile`
- `GET /executions/{execution_id}/runtime/logs`

The isolated control-plane path and the direct worker-container path are both covered by
[`tests/test_docker_worker_integration.py`](../tests/test_docker_worker_integration.py).

## CrewAI Adapter

CrewAI support is treated as an optional runtime adapter over the same canonical workflow contract.

Rules:

- direct CrewAI imports are limited to `app/runtime/adapters/crewai/` and targeted tests
- app services and routes interact with canonical domain models rather than CrewAI objects
- CrewAI-specific wrapping stays in the adapter layer
- CrewAI's built-in memory remains disabled; durable shared memory is currently native-runtime behavior
- migrated tool business logic should remain in `app/tools/implementations`

This keeps the application model independent of any one agent framework.

## Adapter Registry

Runtime selection is adapter-based. The backend can resolve an execution request to:

- native runtime
- CrewAI runtime adapter
- future runtime adapters

The adapter boundary allows the API and persistence layers to stay stable even when execution frameworks change.

Current validation status:

- live end-to-end `native` execution is validated through the canonical execution routes
- live end-to-end `crewai` execution is validated through the same canonical routes
- live CrewAI HITL validation is also covered through `/executions/{execution_id}/hitl/stream` and
  `/executions/{execution_id}/hitl/reply`

## Execution Control Plane

The control plane coordinates:

- execution creation
- status transitions
- heartbeats
- event persistence
- artifact recording
- approval pause and resume behavior
- cancellation and failure reporting

These concerns are shared regardless of which runtime adapter actually executes the workflow.

For Computer Use specifically:

- discovered MCP tools still execute through the normal tool registry and executor stack
- approval-gated desktop mutations use the same approval manager and execution event flow as any other tool
- screenshot-bearing Computer Use responses can attach artifact metadata through the normalized MCP response shape

For isolated native execution, the control plane also:

- resolves the runtime revision snapshot
- persists runtime and container metadata on the execution row
- provisions the worker container
- starts a background watch task for immediate post-exit synchronization
- avoids calling host-side `runtime_registry.start_execution()` after the container has been launched

When the backend/main-agent process runs on the host with `./run.sh start`, this operating model is unchanged
for isolated workflow execution. The chat-facing LLM call stays in the host backend process, while workflow executions
with `EXECUTION_ISOLATION_ENABLED=true` are still delegated to Docker worker containers.

## Isolated Execution Operating Model

The native isolated execution path is implemented and verified.

Current behavior:

- execution containers are created, tracked, replaced, and reconciled
- isolated executions run inside the managed worker container
- the worker owns execution lock, heartbeat, execution events, and final outcome
- worker exit codes are mapped into execution outcomes
- immediate post-exit reconciliation updates execution state and removes completed one-shot containers promptly
- scheduled reconciliation, cleanup policy, runtime metrics, and container log visibility are available

Primary implementation files:

- `app/runtime/control_plane.py`
- `app/runtime/worker.py`
- `app/runtime/worker_protocol.py`
- `app/runtime/reconcile.py`
- `app/runtime/containers.py`
- `app/runtime/native/engine.py`
- `app/api/context.py`
- `app/services/executions.py`
- `app/api/routes/executions.py`
- `app/core/config.py`

Primary verification files:

- `tests/test_runtime_worker.py`
- `tests/test_runtime_reconciler.py`
- `tests/test_execution_control_plane.py`
- `tests/test_docker_worker_integration.py`

## Current Execution Flow

For isolated native executions today:

1. the control plane resolves the runtime revision
2. the control plane persists runtime and container metadata on the execution row
3. the control plane starts the execution container
4. the container launches `python -m app.runtime.worker`
5. the worker claims the execution lock in Postgres
6. the worker runs the native execution engine
7. the worker emits standard execution events into Postgres
8. the worker updates heartbeat while running
9. the worker exits with a meaningful exit code
10. the control plane watcher and reconciler synchronize final container state

For non-isolated and shadow-mode runs, the host execution path still exists and remains valid.

## Execution Lifecycle

Executions carry lifecycle metadata under `execution.metadata.execution_lifecycle`.

- manual and API-created executions default to `run_mode=one_time`
- scheduler-created executions use trigger `type=schedule` and default to `run_mode=scheduled`
- both one-time and scheduled executions default to `terminate_container_on_completion=true`
- future always-on workflows can set workflow metadata
  `execution_lifecycle.terminate_container_on_completion=false`

Schedules are recurring execution requests. They do not keep one container alive between fire times. Each scheduled fire
creates a fresh execution, persists its result to the Agency database, then lets the reconciler remove the finished
worker container unless the workflow explicitly opts out.

## Worker Contract

Required environment passed to the worker:

- `AGENCY_EXECUTION_ID`
- `AGENCY_WORKFLOW_ID`
- `AGENCY_RUNTIME_REVISION_ID`
- `AGENCY_RUNTIME_ADAPTER_ID`
- `DATABASE_URL`

Optional environment:

- `AGENCY_WORKER_ID`
- `AGENCY_HEARTBEAT_INTERVAL_SECONDS`
- `AGENCY_EXECUTION_TIMEOUT_SECONDS`

The worker is responsible for:

- bootstrapping a DB-backed runtime context
- acquiring the execution lock
- running exactly one execution
- updating heartbeats while running
- emitting failure and completion events through the normal execution event stream
- releasing the execution lock on exit

Current exit-code contract:

- `0`: execution completed successfully
- `10`: workflow-level failure
- `20`: worker bootstrap or config failure
- `30`: infrastructure or runtime failure
- `40`: cancelled or replaced

## Runtime Operations

### Reconciliation

The reconciler in `app/runtime/reconcile.py`:

- heals drift between Docker and Postgres
- classifies exited worker containers by exit code
- removes finished execution containers when lifecycle policy allows it
- reaps orphaned managed containers
- enforces container TTL cleanup
- enforces managed image retention cleanup when supported by the runtime manager
- can reconcile all executions periodically or reconcile a single execution immediately after container exit

### Metrics

Runtime operations counters and recent actions are tracked in `app/runtime/operations.py`.

Current examples include:

- reconcile run counts
- reconcile action counts
- container watch completion and failure counts
- container log read counts
- recent runtime actions for operator inspection

### Diagnostics

Worker failures persist structured diagnostics under execution metadata and are surfaced through execution runtime
details.

Current diagnostics include:

- worker error string
- worker id
- runtime revision id
- exception type
- traceback excerpt

## HITL Transport

Human-in-the-loop transport currently uses the channel helpers in `app/runtime/channels.py`.

Behavior:

- when Redis is available, the runtime uses Redis pubsub
- when Redis is unavailable in local or test environments, the runtime falls back to an in-process pubsub implementation

That fallback exists so canonical HITL execution routes can be exercised without requiring a separate Redis daemon for
every local validation run.

Native runtime tool approvals are coordinated by `app/runtime/native/approvals.py`. Workflows that set
`metadata.main_agent_monitoring.delegate_hitl_to_main_agent=true` can delegate low-risk approval-gated tool checkpoints
to the main agent. The delegate provider is wired from `app/api/context.py` and only auto-approves requests without
shell, filesystem, browser, network, MCP, credential, dangerous, mutation, or local privileged risk labels. Delegated
decisions are recorded in approval request rows with `responded_by=main_agent` where SQL approval persistence is
available, and in `approval.granted` event payloads under `decision_metadata`.

### Logs And Operator Endpoints

Operator-facing runtime endpoints currently include:

- `GET /executions/runtime/revisions`
- `GET /executions/runtime/revisions/{revision_id}`
- `GET /executions/runtime/containers`
- `GET /executions/runtime/containers/{container_id}/logs`
- `GET /executions/runtime/metrics`
- `POST /executions/runtime/reconcile`
- `GET /executions/{execution_id}`
- `GET /executions/{execution_id}/runtime/logs`

## Computer Use Operations

Computer Use backends are currently external MCP servers, not in-repo desktop drivers.

Built-in server definitions seeded by the backend:

- `computer-use-macos`
- `computer-use-windows`

Default commands:

- macOS: `uvx macos-mcp`
- Windows: `uvx windows-mcp`

Runtime expectations:

- only the host-compatible Computer Use backend is auto-synced on startup by default
- normalized Computer Use tools are persisted as canonical `ToolDefinition` rows under ids like
  `mcp:computer-use-macos:click`
- desktop mutation tools should remain approval-gated
- execution events and approval events remain the audit trail for Computer Use actions

Artifact behavior:

- `snapshot` and `screenshot` normalization may emit `artifact_uri`, `artifact_name`, `artifact_type`, and
  `artifact_media_type`
- native runtime artifact recording can persist those references without embedding raw binary payloads in execution rows

### Mount And Database Policy

The isolated runtime supports a distinct container-visible DB URL and explicit extra runtime mounts.

Important settings:

- `WORKFLOW_SCHEDULER_ENABLED`
- `WORKFLOW_SCHEDULER_INTERVAL_SECONDS`
- `WORKFLOW_RESTART_ACTIVE_EXECUTIONS_ON_REVISION_CHANGE`
- `CANCEL_OUTDATED_EXECUTIONS`
- `RUNTIME_REVISION_SHADOW_MODE`
- `EXECUTION_RUNTIME_DATABASE_URL`
- `EXECUTION_RUNTIME_BASE_IMAGE`
- `EXECUTION_CONTAINER_NETWORK`
- `EXECUTION_CONTAINER_WORKDIR`
- `EXECUTION_CONTAINER_MEMORY_LIMIT_MB`
- `EXECUTION_CONTAINER_CPU_LIMIT`
- `EXECUTION_CONTAINER_AUTO_REMOVE`
- `EXECUTION_CODEX_CLI_CWD`
- `EXECUTION_CONTAINER_EXTRA_MOUNTS`
- `EXECUTION_CONTAINER_BIND_INTEGRATIONS_READ_ONLY`
- `RUNTIME_CONTAINER_TTL_SECONDS`
- `RUNTIME_IMAGE_RETENTION_COUNT`
- `RUNTIME_MAX_CONCURRENT_BUILDS`
- `RUNTIME_RECONCILER_INTERVAL_SECONDS`

For trusted local Docker development, the default backend and frontend workspace mounts are read-write so coding
workflows can edit repos inside worker containers. Agency probes visible read-write mount paths before container launch;
if the backend user cannot write, the launch fails with a permission prompt instead of surfacing a later `Errno 30`
read-only filesystem error from the agent. Keep production isolation locked down and use read-only mounts unless a
workflow explicitly needs repo edits.

Read-write repo access checklist:

1. Mount trusted local repos with `:rw` in `docker-compose.yml` or `EXECUTION_CONTAINER_EXTRA_MOUNTS`.
2. Use stable worker targets, such as the values configured in `AGENCY_BACKEND_WORKSPACE` / `AGENCY_FRONTEND_WORKSPACE`
   or another dedicated container path like `/repo/other-repo`.
3. Confirm the host user running Docker can write to the source directory.
4. Recreate the backend container after changing bind mounts or monitor env vars.
5. Let workflow proposals request repo write permission before launch; do not grant repo writes implicitly for
   high-risk or privileged workflows.

Example extra mount for a second repo:

```json
[
  {
    "source": "/Users/example/Documents/other-repo",
    "target": "/repo/other-repo",
    "read_only": false
  }
]
```

Troubleshooting a read-only worker:

- If the agent reports `OSError: [Errno 30] Read-only file system`, inspect the worker mount mode first.
- If the mount is `:ro`, change it to `:rw` only for trusted local development or an explicitly approved coding
  workflow.
- If the mount is already `:rw`, check host filesystem ownership/ACLs and Docker Desktop file-sharing permissions.
- Recreate the backend after the fix so new workers inherit the updated mount configuration.

After changing compose mount mode or monitor environment settings in a running local stack, recreate the backend
container so Docker applies the new bind mounts and environment:

```bash
docker compose up -d --force-recreate backend
```

Production and operator deployments should also configure `AGENCY_INTERNAL_API_KEY` so trusted identity headers cannot
be spoofed by untrusted callers. When paired with `open-agency-fe`, set the matching frontend server-side value as
`AGENCY_FE_BFF_IDENTITY_KEY`; this key is scoped to explicit BFF session delegation and is not a direct operator token.

Remote browser clients such as `open-agency-fe` must be listed in `AGENCY_ALLOWED_ORIGINS` using exact origins. Production
deployments should not use wildcard CORS origins, especially while `AGENCY_CORS_ALLOW_CREDENTIALS=true`.

Connector health retention is controlled by:

- `CONNECTOR_HEALTH_HISTORY_RETENTION_ENABLED`
- `CONNECTOR_HEALTH_HISTORY_RETENTION_INTERVAL_SECONDS`
- `CONNECTOR_HEALTH_HISTORY_RETENTION_DAYS`
- `CONNECTOR_HEALTH_HISTORY_RETENTION_MAX_PER_CREDENTIAL`

## Deferred Scope

The following are intentionally deferred:

- isolated-host execution for non-native adapters such as CrewAI
- broader adapter capability negotiation for isolated mode
- environment-specific production rollout sequencing and external operational alerting outside the codebase

## Future OpenAI Agents Adapter

A future OpenAI Agents adapter should follow the same pattern:

- map canonical workflow and agent definitions into adapter-specific objects
- keep provider-specific orchestration inside `app/runtime/adapters`
- emit canonical `ExecutionEvent` records
- reuse existing repositories, tool registry, and approval flows

## Future NVIDIA NeMo Adapter

A future NVIDIA NeMo or similar orchestration adapter should follow the same constraints:

- no framework-specific models in `app/domain`
- no direct route coupling to adapter internals
- canonical persistence through repositories and execution stores
- adapter-specific availability checks and capability exposure through runtime adapter records
