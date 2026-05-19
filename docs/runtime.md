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

Shared memory is retrieved before each native task when workflow metadata has `shared_memory.enabled=true` or the assigned
agent has `memory.enabled=true` with a non-`execution` scope. Retrieval uses the canonical memory service and can draw
from relevant workflow, workspace, user, global, and agent-attributed records. Operators can update the setting through
`PATCH /workflows/{workflow_id}/shared-memory`.

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
- `EXECUTION_RUNTIME_DATABASE_URL`
- `EXECUTION_CONTAINER_NETWORK`
- `EXECUTION_CONTAINER_WORKDIR`
- `EXECUTION_CODEX_CLI_CWD`
- `EXECUTION_CONTAINER_EXTRA_MOUNTS`
- `EXECUTION_CONTAINER_BIND_INTEGRATIONS_READ_ONLY`
- `RUNTIME_CONTAINER_TTL_SECONDS`
- `RUNTIME_IMAGE_RETENTION_COUNT`
- `RUNTIME_RECONCILER_INTERVAL_SECONDS`

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
