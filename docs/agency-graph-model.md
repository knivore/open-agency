# Agency Graph Model

Agency Graph is the shared graph language for Agency operations, knowledge, lineage, health, governance, cost, and
debugging. It is consumed by visualization clients, observability APIs, and future agent graph-context tools.

This document defines the canonical model contract. Neo4j projection code may keep legacy labels and relationship names
during migration, but API responses, UI language, docs, and agent tools should converge on the canonical vocabulary
below.

## Goals

- Give backend, frontend, Neo4j projection, and agent tools one graph vocabulary.
- Keep graph projection read-only with respect to source-of-truth runtime and memory records.
- Preserve lineage from graph nodes and edges back to canonical records.
- Keep sensitive payloads summarized and redacted.
- Support bounded graph reads for UI and agent runtime use.
- Avoid exposing raw Neo4j records or arbitrary Cypher to agents.

## Consumers

- Agency Graph UI: interactive Sigma-based graph investigation.
- Observability APIs: execution and workflow graph views.
- Agent graph-context tools: compact context for planning, steering, debugging, and resume.
- Projection diagnostics: parity, lag, failed projection, and graph health checks.

## Canonical Node Types

| Type                | Represents                                                                    | Current Neo4j compatibility                                                               |
|---------------------|-------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| `User`              | User or owner boundary.                                                       | `User`                                                                                    |
| `Goal`              | Durable objective that can span plans, workflow attempts, evidence, and approvals. | Not projected yet                                                                         |
| `Workflow`          | Workflow definition.                                                          | `Workflow`                                                                                |
| `WorkflowVersion`   | Versioned workflow definition or revision snapshot.                           | Not projected yet                                                                         |
| `Schedule`          | Schedule that can trigger workflow runs.                                      | Not projected yet                                                                         |
| `Run`               | Workflow execution/run.                                                       | `WorkflowRun`                                                                             |
| `ExecutionWait`     | Durable input, approval, event, or sleep suspension for a run.                | Wait and wake events project as `ExecutionEvent`; dedicated wait node not projected yet   |
| `Agent`             | Agent definition or runtime participant.                                      | `Agent`                                                                                   |
| `Task`              | Workflow task definition.                                                     | `Task`                                                                                    |
| `StepRun`           | Runtime execution of a task/step.                                             | `StepRun`                                                                                 |
| `Tool`              | Tool definition or callable capability.                                       | `Tool`                                                                                    |
| `ToolCall`          | Runtime call of a tool.                                                       | Projected from selected tool call execution events                                        |
| `ModelProvider`     | Model provider.                                                               | Projected from runtime model request observations when provider is available              |
| `Model`             | Model profile or model identity.                                              | Projected from workflow agent model profiles and runtime model request observations       |
| `ModelRequest`      | Runtime model request/response observation.                                   | Projected from selected LLM execution events                                              |
| `RuntimeRevision`   | Runtime code/config revision used for a run.                                  | Projected when runtime revision metadata is available                                     |
| `RuntimeContainer`  | Worker/container instance used for execution.                                 | Projected from execution/container events                                                 |
| `ExecutionEvent`    | Ordered runtime event.                                                        | Projected for selected runtime, tool, model, approval, artifact, and observability events |
| `ContainerEvent`    | Container lifecycle event derived from execution events.                      | Projected from selected container events                                                  |
| `Artifact`          | File, result, or output artifact produced by execution.                       | Projected from artifact execution events                                                  |
| `Memory`            | Durable memory record.                                                        | `Memory`                                                                                  |
| `ContextPack`       | Durable compact context pack memory.                                          | Currently `Memory` with `memory_type=context_pack`                                        |
| `Conversation`      | Conversation/thread.                                                          | `Conversation`                                                                            |
| `Message`           | Conversation message.                                                         | Not projected yet                                                                         |
| `Document`          | Uploaded or ingested source document.                                         | `Document`                                                                                |
| `DocumentChunk`     | Chunk of a source document.                                                   | Currently represented as `Memory` archive chunks                                          |
| `Entity`            | Extracted entity mention target.                                              | `Entity`                                                                                  |
| `Decision`          | Structured decision extracted from a conversation, run, or context pack.      | Not projected yet                                                                         |
| `Constraint`        | Constraint, rule, requirement, or boundary.                                   | Not projected yet                                                                         |
| `OpenQuestion`      | Open question or unresolved issue.                                            | Not projected yet                                                                         |
| `NextAction`        | Suggested or committed next action.                                           | Not projected yet                                                                         |
| `Finding`           | Monitor, evaluation, health, or quality finding.                              | `MonitorFinding` for monitor finding events                                               |
| `ContextHealth`     | Context-window pressure snapshot.                                             | `ContextHealth`                                                                           |
| `TokenUsage`        | Token and estimated cost usage observation.                                   | `TokenUsage`                                                                              |
| `TokenBudget`       | Token budget warning or exceeded signal.                                      | `TokenBudget`                                                                             |
| `ContextCompaction` | Runtime context compaction attempt or result.                                 | `ContextCompaction`                                                                       |
| `Error`             | Runtime, projection, tool, or model error.                                    | Projected for run, step, and execution event failures                                     |
| `ApprovalRequest`   | Human approval request and outcome.                                           | Approval events project as `ExecutionEvent`; dedicated request node not projected yet     |
| `Integration`       | External connector, MCP server, A2A agent, or integration.                    | Not projected yet                                                                         |
| `Credential`        | Credential reference or governed secret handle. Values must not be projected. | Not projected yet                                                                         |

## Canonical Relationship Types

| Type                      | Meaning                                                      | Current Neo4j compatibility                                                                      |
|---------------------------|--------------------------------------------------------------|--------------------------------------------------------------------------------------------------|
| `CREATED_BY`              | Record was created by a user or actor.                       | `CREATED_MEMORY`, `OWNS_DOCUMENT` for specific cases                                             |
| `HAS_GOAL`                | Record is scoped to or advancing a durable goal.              | Not projected yet                                                                                |
| `ATTEMPTED_BY`            | Goal was advanced by a workflow run/execution attempt.        | Not projected yet                                                                                |
| `HAS_EVIDENCE`            | Goal has linked evidence, artifact, approval, or evaluation.  | Not projected yet                                                                                |
| `STARTED`                 | Workflow started a run.                                      | `HAS_RUN`                                                                                        |
| `TRIGGERED`               | Schedule or event triggered a run.                           | Projected from execution trigger payload when schedule id is available                           |
| `PARTICIPATED_IN`         | Agent participated in a run or step.                         | Projected from step and execution detail events                                                  |
| `OCCURRED_IN`             | Task, tool call, model request, or event occurred in a run.  | Projected for tasks, tool calls, and model requests where ids are available                      |
| `ASSIGNED_TO`             | Task or step assigned to an agent.                           | Projected from workflow definitions and step runs                                                |
| `DEPENDS_ON`              | Task, step, or run dependency.                               | Projected from workflow task definitions                                                         |
| `CAN_USE`                 | Agent/workflow/task can use a tool.                          | Projected from workflow agent definitions                                                        |
| `CAN_HANDOFF_TO`          | Agent handoff capability.                                    | Projected from workflow agent definitions                                                        |
| `CALLED_TOOL`             | Run, step, or event called a tool.                           | Projected from tool call execution events                                                        |
| `USED_MODEL`              | Run, agent, or model request used a model.                   | Projected; `USES_MODEL_PROFILE` is also emitted for agent model-profile compatibility            |
| `USED_PROVIDER`           | Model used a provider.                                       | Projected from model request provider/model observations                                         |
| `USED_RUNTIME`            | Run used a runtime revision or adapter.                      | Projected from execution/run events where runtime revision is available                          |
| `CREATED_CONTAINER`       | Run created/used a runtime container.                        | Projected from execution and container events                                                    |
| `EMITTED_EVENT`           | Run emitted an execution event.                              | Projected for selected execution, tool, model, runtime, approval, artifact, and container events |
| `HAS_WAIT`                | Run has a durable execution wait.                           | Wait and wake events project through `EMITTED_EVENT`; dedicated relationship not projected yet  |
| `FOLLOWED_BY`             | Event sequence ordering.                                     | Not projected yet                                                                                |
| `PARENT_OF`               | Event or task parent-child relation.                         | Projected for execution events with parent ids                                                   |
| `FAILED_WITH`             | Run, step, tool call, or model request failed with an error. | Projected for run, step, and execution event failures                                            |
| `PRODUCED_ARTIFACT`       | Run, step, or tool call produced an artifact.                | Projected from artifact execution events                                                         |
| `HAS_CONTEXT_HEALTH`      | Run has a context health snapshot.                           | `HAS_CONTEXT_HEALTH`                                                                             |
| `RECORDED_CONTEXT_HEALTH` | Event recorded context health.                               | `RECORDED_CONTEXT_HEALTH`                                                                        |
| `RECORDED_USAGE`          | Run, model request, or event recorded token/cost usage.      | `RECORDED_USAGE`                                                                                 |
| `HAS_BUDGET_SIGNAL`       | Run or event has a token budget signal.                      | `HAS_BUDGET_SIGNAL`                                                                              |
| `HAS_COMPACTION`          | Run or event has a context compaction signal.                | `HAS_COMPACTION`                                                                                 |
| `RAISED_FINDING`          | Run or event raised an operational finding.                  | `RAISED_FINDING`                                                                                 |
| `CREATED_MEMORY`          | Actor/source created a memory.                               | `CREATED_MEMORY`                                                                                 |
| `DERIVED_FROM`            | Derived record came from another record.                     | Not projected yet                                                                                |
| `SOURCE_EXECUTION`        | Memory or derived record came from a run/execution.          | `SOURCE_EXECUTION`                                                                               |
| `SOURCE_CONVERSATION`     | Memory or context pack came from a conversation.             | `SOURCE_CONVERSATION`                                                                            |
| `SOURCE_DOCUMENT`         | Memory/chunk/entity came from a document.                    | Property today, edge not generally projected                                                     |
| `HAS_CHUNK`               | Document has chunks.                                         | `HAS_CHUNK` from `Document` to chunk `Memory`                                                    |
| `PART_OF_DOCUMENT`        | Chunk belongs to a document.                                 | `PART_OF_DOCUMENT`                                                                               |
| `MENTIONS`                | Memory/document/message mentions an entity.                  | `MENTIONS` from `Memory` to `Entity`                                                             |
| `SUPPORTS_DECISION`       | Evidence supports a decision.                                | Not projected yet                                                                                |
| `CONSTRAINS`              | Constraint applies to a workflow, task, agent, or run.       | Not projected yet                                                                                |
| `RAISED_QUESTION`         | Record raised an open question.                              | Not projected yet                                                                                |
| `SUPERSEDES`              | Record supersedes an older record.                           | `SUPERSEDES`                                                                                     |
| `AVAILABLE_TO`            | Memory/document is available to a scope target.              | `AVAILABLE_TO`                                                                                   |
| `LINKS_MEMORY`            | Workflow links a memory resource.                            | `LINKS_MEMORY`                                                                                   |
| `HAS_MEMORY_LINK`         | Workflow/agent/task has an explicit memory link.             | `HAS_MEMORY_LINK`                                                                                |
| `HAS_APPROVAL`            | Run/proposal/tool call has an approval request.              | Not projected yet                                                                                |
| `USES_INTEGRATION`        | Tool, workflow, or agent uses an integration.                | Not projected yet                                                                                |
| `DEFINES_AGENT`           | Workflow defines an agent.                                   | `DEFINES_AGENT`                                                                                  |
| `DEFINES_TASK`            | Workflow defines a task.                                     | `DEFINES_TASK`                                                                                   |
| `DEFINES_TOOL`            | Workflow defines a tool.                                     | `DEFINES_TOOL`                                                                                   |
| `HAS_STEP_RUN`            | Run contains a step run.                                     | `HAS_STEP_RUN`                                                                                   |

## Compatibility Rules

The canonical model should not force an immediate destructive Neo4j rename. During migration:

- `WorkflowRun` remains the Neo4j label for canonical `Run`.
- `StepRun` remains a concrete runtime node type, distinct from task definition `Task`.
- Goal nodes and goal relationships are canonical vocabulary now, even if current graph projection still derives most
  goal context from run metadata, execution events, and memory/evidence records.
- Durable wait rows remain runtime source-of-truth records. `execution.waiting` and `execution.woken` project as
  `ExecutionEvent` nodes; a dedicated `ExecutionWait` node and `HAS_WAIT` relationship can be added without changing
  the execution lifecycle contract.
- Persistent monitor cycle events project as `ExecutionEvent` nodes. Cycle counters and progress signatures remain
  execution metadata until a dedicated cycle node has a demonstrated graph-query use case.
- `Memory` archive chunks remain compatible with canonical `DocumentChunk` until a separate `DocumentChunk` projection is added.
- `SOURCE_EXECUTION` remains the projected relationship name for source run/execution lineage.
- Existing `/graph/read/*` routes remain stable and can return legacy labels in `labels`.
- Higher-level services may expose canonical `type` values while preserving raw labels for compatibility.
- UI and agent-facing copy should say `Agency Graph`, not `Memory Graph`.
- Sigma remains a renderer/client detail and should not appear in backend graph model names.

## Preset Naming Policy

Graph presets should use canonical, user-facing names at the API and agent-tool boundary. During migration, each preset
may translate canonical names into current Neo4j labels and relationship types internally.

Recommended policy:

- Public preset ids are canonical snake case, for example `run`, `workflow`, `memory_provenance`, `failed_run_root_cause`,
  `subagent_steering`, and `coding_agent_resume`.
- Existing preset ids such as `workflow_run` remain compatibility aliases until clients migrate.
- Public mode names are canonical: `operational`, `knowledge`, `lineage`, `health`, `cost`, and `security`.
- Internal Neo4j queries may include both legacy and canonical relationship names where migration overlap is expected.
- Response `meta` should include the requested canonical preset and any resolved legacy labels/relationships used.
- Agent tools should accept only canonical preset/mode names unless they explicitly expose a compatibility field.

Initial compatibility mapping:

| Canonical preset/mode | Legacy/internal labels                                        | Legacy/internal relationships                                                                                           |
|-----------------------|---------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------|
| `workflow`            | `Workflow`                                                    | `HAS_RUN`, `HAS_STEP_RUN`, `DEFINES_AGENT`, `DEFINES_TASK`, `DEFINES_TOOL`, `HAS_MEMORY_LINK`, `LINKS_MEMORY`           |
| `run`                 | `WorkflowRun`                                                 | `HAS_RUN`, `HAS_STEP_RUN`, `SOURCE_EXECUTION`, `LINKS_MEMORY`, `HAS_CHUNK`, `PART_OF_DOCUMENT`                          |
| `agent`               | `Agent`                                                       | `DEFINES_AGENT`, `PERFORMED_BY`, `ASSIGNED_TO`, `CAN_USE`, `CAN_HANDOFF_TO`, `USES_MODEL_PROFILE`, `HAS_STEP_RUN`       |
| `task`                | `Task`, `StepRun`                                             | `DEFINES_TASK`, `HAS_STEP_RUN`, `ASSIGNED_TO`, `DEPENDS_ON`, `USES_TOOL`, `AVAILABLE_TO`, `PERFORMED_BY`                |
| `memory_provenance`   | `Memory`, `Document`, `Conversation`, `WorkflowRun`, `Entity` | `SOURCE_EXECUTION`, `SOURCE_CONVERSATION`, `SOURCE_DOCUMENT`, `HAS_CHUNK`, `PART_OF_DOCUMENT`, `MENTIONS`, `SUPERSEDES` |
| `document_lineage`    | `Document`, `Memory`, `Entity`                                | `HAS_CHUNK`, `PART_OF_DOCUMENT`, `MENTIONS`, `AVAILABLE_TO`                                                             |

## Event-Derived Fallback Graph

The frontend Agency Graph can synthesize a graph from execution records and `/executions/{execution_id}/events` when
Neo4j projection is unavailable or has not caught up. This is useful for immediate failed-run diagnosis, but it is not
durable graph projection.

Fallback graph rules:

- Fallback documents must set `projection_mode` to `execution-events-fallback`.
- Fallback documents must set `root_type` and `root_id`.
- Fallback nodes and edges should use canonical Agency Graph types.
- Fallback nodes must preserve source ids in metadata such as `sourceRecordId`.
- Fallback graphs are display and short-term diagnostic artifacts.
- Fallback graphs must not be written into Neo4j as if they were projected records.
- Durable projection should eventually produce equivalent or richer nodes and edges from the same source records.
- Reconciliation logic must avoid duplicate nodes when durable projection catches up.

Current frontend fallback mapping:

| Source data                    | Fallback node/edge                                                       |
|--------------------------------|--------------------------------------------------------------------------|
| execution record               | `Run` node                                                               |
| execution workflow id          | `Workflow STARTED Run`                                                   |
| schedule id in trigger payload | `Schedule TRIGGERED Run`                                                 |
| runtime revision id            | `Run USED_RUNTIME RuntimeRevision`                                       |
| container id/name              | `Run CREATED_CONTAINER RuntimeContainer`                                 |
| execution error                | `Run FAILED_WITH Error`                                                  |
| execution metadata agent ids   | `Agent PARTICIPATED_IN Run`                                              |
| execution event                | `ExecutionEvent` or `ContainerEvent` node plus `Run EMITTED_EVENT Event` |
| event sequence                 | `ExecutionEvent FOLLOWED_BY ExecutionEvent`                              |
| parent event id                | `ExecutionEvent PARENT_OF ExecutionEvent`                                |
| event agent id                 | `Agent EMITTED_EVENT ExecutionEvent`                                     |
| event task id                  | `Task OCCURRED_IN Run`                                                   |
| event tool call id             | `ToolCall OCCURRED_IN Run`                                               |
| event model request id         | `ModelRequest OCCURRED_IN Run`                                           |
| artifact event or payload      | `Run PRODUCED_ARTIFACT Artifact`                                         |

## Agent Use Of Fallback Data

Agents should prefer durable Neo4j projection for graph context. When Neo4j is disabled or unavailable, an agent graph
context service may use execution APIs as a bounded fallback only for runtime/debug intents.

Allowed fallback use:

- `debug`
- `root_cause`
- `steer`
- `resume` when anchored to a specific run/execution

Disallowed fallback use:

- broad knowledge search
- durable memory provenance when memory projection is unavailable
- cross-workflow graph navigation
- graph-derived context-pack persistence without explicitly marking fallback provenance

If fallback data is used in an agent response, the tool output must include:

- `fallback_used: true`
- `projection_available: false` or the current projection status
- source API names used, such as `/executions/{id}` and `/executions/{id}/events`
- clear provenance for each fallback-derived fact
- no claim that the result came from durable Neo4j projection

## Node Identity

Every node must have a stable `id`.

Recommended id sources:

- source record id when there is a single canonical record
- deterministic composite id for derived nodes
- prefix only when needed to avoid collision between record domains

Examples:

- `workflow_id` for `Workflow`
- `execution_id` for canonical `Run`
- `task_id` for task definitions
- `execution_id:task_id` or another deterministic run-step key for `StepRun` when available
- `memory_id` for `Memory`
- `document_id` for `Document`
- deterministic entity id from entity extraction for `Entity`
- `event_id` for `ExecutionEvent`
- `tool_call_id` for `ToolCall`

## Edge Identity

Every edge must have a stable `id` when exposed through DTOs.

Recommended edge id format:

```text
{source}:{relationship_type}:{target}
```

Use a deterministic qualifier for multi-edge cases:

```text
{source}:{relationship_type}:{target}:{qualifier}
```

Examples:

- workflow memory links should include `link_id`
- entity mentions should include extractor version or mention id
- event ordering should include event sequence or event id

## Source Metadata

Graph nodes and edges should carry enough source metadata for provenance and debugging.

Recommended properties:

- `source_system`: usually `agency`
- `source_record_type`: canonical source table or domain record type
- `source_record_id`: source record id
- `source_event_id`: graph projection outbox event id when applicable
- `source_endpoint`: read endpoint when graph data is synthesized from an API
- `projection_mode`: `neo4j`, `execution-events-fallback`, `synthetic`, or `manual`
- `projection_version`: projection schema/version when applicable
- `projected_at`: when the graph projection was written
- `generated_at`: when a synthetic graph response was generated

## Query Metadata

Graph responses should include query metadata in `meta`.

Recommended fields:

- `query`
- `root_type`
- `root_id`
- `intent`
- `mode`
- `depth`
- `limit`
- `node_count`
- `edge_count`
- `truncated`
- `omitted`
- `projection_available`
- `fallback_used`
- `include_deleted`

## Health Metadata

Graph nodes may include health metadata when available:

- `status`
- `severity`
- `last_seen_at`
- `stale`
- `missing_embedding`
- `sensitive`
- `deleted`
- `cost_estimate`
- `token_count`

These fields are optional, but consumers should preserve them when present.

## Redaction

Graph projection and graph reads must not expose:

- API keys
- authorization headers
- credential values
- passwords
- tokens
- secrets
- raw embeddings
- hidden credential references
- raw sensitive payloads
- raw uploaded document content unless a policy explicitly permits it

Projected nodes should prefer summaries, ids, types, statuses, timestamps, provenance, and bounded snippets over raw
payload dumps.

Agent-facing graph context applies an additional safety layer: credential, token, auth-session, connector-account, and
external-account nodes are omitted with connected edges, while integration and connector nodes are represented only as
protected placeholders with safe health, status, and provenance metadata.

Workflow definition projection is topology-first. It may project workflow, agent, task, and tool identifiers; names;
descriptions; role/model ids; memory flags; schema-presence booleans; security/capability flags; and relationships such
as `DEFINES_AGENT`, `DEFINES_TASK`, `DEFINES_TOOL`, `ASSIGNED_TO`, `DEPENDS_ON`, `CAN_USE`, `CAN_HANDOFF_TO`, and
`USES_TOOL`. It must not project raw agent instructions, system prompts, backstories, task instructions, expected output,
tool implementation config, or credential material.

Runtime observability projection is metric-first. It may project token counts, estimated cost, context pressure,
compaction status, budget status, model ids, provider ids, and monitor finding summaries. It must not project prompt
messages, response content, provider raw payloads, or detailed tool outputs.

## Graph Read Invariants

- Graph read APIs return normalized DTOs, not Neo4j driver records.
- Graph reads are read-only.
- Traversal is bounded by depth, node/edge count, output size, and timeout.
- Deleted and superseded records are hidden by default.
- Sensitive records are hidden or summarized by policy.
- Graph projection does not mutate source-of-truth records.
- Event-derived fallback graph data is marked as fallback and must not be confused with durable Neo4j projection.

## Agent Tool Invariants

Future graph-context tools must follow these rules:

- First tool is `agency.graph.context`.
- Tools are read-only in the first release.
- Tools do not expose arbitrary Cypher.
- Tools use canonical Agency Graph terms in inputs and outputs.
- Tools include provenance for high-signal facts.
- Tools return compact context before raw graph DTOs.
- Tools degrade cleanly when Neo4j is disabled or unavailable.
- Tools respect memory visibility and memory exclusions.
- Tools log runtime and policy events.

## Migration Priorities

1. Stabilize this canonical model contract.
2. Extend Neo4j projection to cover operational graph data.
3. Add query primitives for search, expand, path, and modes.
4. Build deterministic graph context synthesis.
5. Expose `agency.graph.context`.
6. Add runtime auto-retrieval only after manual tool calls are useful.
