# Architecture

## Overview

The backend is organized around the `app/` package. The target architecture is:

- canonical domain models
- adapter-based runtimes
- Postgres-backed persistence
- explicit tool security and approval controls
- protocol boundaries for MCP and A2A

The codebase is now centered on `app/`. Legacy root architecture folders have been removed, with compatibility
constrained to app-owned adapter and route layers.

Current documentation boundary:

- active architecture and contract guidance lives in the current `docs/*.md` files
- main-agent chat planning and model-auth recovery are covered in [docs/main-agent.md](./main-agent.md)
- migration trackers, TODO playbooks, and archive snapshots are intentionally excluded from the maintained documentation
  set

Optional module loading is explicit. Core deployments start without add-on packs unless `AGENCY_BUILTIN_OPTIONAL_MODULES`
or `AGENCY_OPTIONAL_MODULE_SPEC_REFS` names module specs to load; module-specific migrations should stay with their
own packs instead of being chained into the core Alembic history.

## app/domain

Purpose:

- defines the canonical backend model
- keeps workflow, execution, tool, schedule, and provider concepts independent from any one framework

Important models:

- `AgentDefinition`
- `ToolDefinition`
- `WorkflowDefinition`
- `Execution`
- `ExecutionWait`
- `ExecutionEvent`
- `ScheduleDefinition`
- `MCPServerDefinition`

## app/db

Purpose:

- owns persistence
- provides SQLAlchemy ORM models and async session management
- exposes repositories used by API, service, scheduler, and runtime layers

Important areas:

- `base.py`
- `session.py`
- `models/`
- `repositories/`
- `mongo.py` for limited compatibility support

## app/api

Purpose:

- assembles FastAPI
- defines HTTP contracts
- keeps route handlers thin

Main areas:

- `main.py`
- `context.py`
- `routes/`
- `schemas/`

## app/runtime

Purpose:

- owns execution behavior
- isolates framework-specific execution behind runtime adapters

Main areas:

- `native/`
- `adapters/`
- `control_plane.py`

Current adapters:

- `native`
- `crewai`

### Runtime Lifecycles

A goal, workflow, and execution have separate lifetimes. A goal is a durable objective, a workflow is a reusable plan,
and an execution is one stateful run of that plan. Long-running workflow support belongs to the execution control plane
and uses three forms:

- waitable executions suspend for input, approval, an event, or a wake time and resume from a durable checkpoint
- persistent monitor executions repeat bounded, auditable cycles under one execution id until policy or an operator
  stops them
- goal-driven recurring workflows use multiple finite executions under one durable goal

`ExecutionWait` is the durable wait ledger. `Execution.status` is the current-state view, while ordered
`ExecutionEvent` records remain the runtime audit source of truth. Input, event, and sleep waits can resume a native
execution from persisted node outputs after backend restart. Approval-gated tool calls additionally persist the current
agent transcript, remaining tool calls, and pending call position so the worker can exit and a later worker can resume
at that tool boundary without replaying completed calls from the same model response.

Native persistent monitors use sleep waits between full graph cycles rather than keeping a worker alive. Cycle start,
completion, failure, and guard events are projected like other execution events; bounded cycle state and recent outcomes
remain on execution metadata, while the wait row owns the next timer claim.

## app/tools

Purpose:

- defines tools as app-owned records
- validates tool security
- dispatches execution through typed executors

Main areas:

- `definitions.py`
- `registry.py`
- `validation.py`
- `executors/`
- `implementations/`

## app/browser_runtime

Purpose:

- owns the authenticated Patchright-first browser sidecar and bounded Scrapling fallback
- retains multiple owner-scoped live sessions across agent calls and durable human waits
- applies outbound-host policy, challenge classification, extraction, resource limits, and artifact retention
- clamps per-open agent resource preferences to local operator-configured limits

The public capability remains the `agency.browser.*` tool family under `app/tools`; raw browser handles, cookies,
profiles, proxy credentials, and the runtime signing secret never cross that boundary.

## app/llm

Purpose:

- implements provider-specific model clients
- resolves model profiles into runtime clients

Examples:

- OpenAI-compatible
- Anthropic
- Google
- Bedrock

## app/scheduler

Purpose:

- owns schedule creation, enable/disable, next-fire calculation, and manual triggering
- creates executions through the runtime registry

## app/protocols

Purpose:

- isolates protocol-specific integration logic

Subareas:

- `mcp/`
- `a2a/`

## app/observability

Purpose:

- computes execution timelines and metrics
- aggregates canonical execution events into API-facing telemetry

Core principle:

- `ExecutionEvent` is the internal source of truth for execution history

## Agency Graph

Agency Graph is the shared graph model for operational, memory, lineage, health, governance, cost, and debugging views.
Neo4j is the durable graph projection backend, while Sigma and other clients are consumers of normalized graph DTOs.

Current projection support lives under `app/graph/`. Existing Neo4j labels such as `WorkflowRun`, `StepRun`, `Memory`,
`Document`, and `Entity` remain compatibility labels during the Agency Graph migration. User-facing docs, UI, and future
agent tools should use the canonical model in [docs/agency-graph-model.md](./agency-graph-model.md).

Graph projection is read-only with respect to source-of-truth runtime, workflow, conversation, and memory records.
Graph reads must stay bounded, redacted, and normalized so they can safely serve UI, observability, and future
agent-context tools.

`agency.graph.context` is the first agent-facing Agency Graph tool. It is enabled for discovery and seed data through
`AGENCY_GRAPH_CONTEXT_TOOLS_ENABLED` and remains read-only. Agents can call it with a known graph anchor or a bounded
query to retrieve facts, related memories, execution context, failures, decisions, constraints, next actions, provenance,
and optional raw graph DTOs. This complements durable/vector memory: durable memory recalls stored semantic facts, while
Agency Graph context explains relationships, lineage, operational history, and why a run or workflow is connected to
other records.

## Memory Architecture

Memory is database-backed.

- raw `main-agent` interaction history lives in `conversations` and `conversation_messages`
- durable memory for all agents lives in `memory_records`
- runtime scratch state remains ephemeral unless a service explicitly writes a durable summary

Current memory services:

- `MemoryService` owns durable-memory CRUD, retrieval, ranking, and summary helpers
- `ConversationDailySummaryService` writes durable `daily_summary` records for main-agent conversations
- `ExecutionRunSummaryService` writes optional durable `run_summary` records for non-main-agent executions

This design keeps the source of truth in the database and avoids file-based memory concurrency issues.

## Persona Factory

Persona Factory is the DB-backed bounded context for converting source material into reusable Agency personas. `Persona`
is the canonical Agency product and API term; other agent ecosystems may call similar reusable packages "skills". It
composes existing primitives rather than replacing them:

- persona records and versions are the governed persona package source of truth
- selected source memories and uploaded document chunks provide provenance
- approved packages can publish an `AgentDefinition`
- package memory layers are written back into `memory_records` with persona metadata
- future graph projection can add `Persona`, `PersonaVersion`, and source-lineage nodes without changing runtime records

Initial product APIs live under `/persona` and `/persona-factory`. `/skills`, `/skill-factory`, and `/personas`
compatibility endpoints are intentionally not exposed. The first distillation strategy is deterministic and review-first;
LLM extraction can be added behind the same package schema after structured-output validation and approval controls are
in place.

Persona Factory is separate from tool management. A tool is an executable capability with a schema, permission model,
and executor. A persona is reusable simulated identity and operating style: voice, knowledge, judgement, workflow
patterns, guardrails, and source-backed memory. Professional expertise is a subtype of persona, not the whole model.
Published personas can materialize an `AgentDefinition` and bind to tools, but Persona Factory owns the governed package
lifecycle while tool management owns callable side effects.

For backend developer guidance, see `docs/persona-factory.md`. For backend-only creation, review, approval, publish,
and runtime invocation examples, see `docs/persona-factory-cli.md`. Recurring operator commands belong in
`docs/runbook.md`.

## app/security

There is no dedicated `app/security/` package yet. Security is currently cross-cutting:

- tool permission and approval metadata in `app/domain/tools.py`
- validation rules in `app/tools/validation.py`
- approval execution flow in `app/runtime/native/approvals.py`
- runtime and API boundaries around compatibility code

## Remaining Compatibility Seams

Still intentionally present:

- CrewAI adapter helpers under `app/runtime/adapters/crewai`
- canonical workflow and execution routes that select `crewai` as a runtime adapter when requested
- a local/test in-process HITL pubsub fallback in `app/runtime/channels.py` when Redis is unavailable

Do not design new frontend or integration code around legacy `/api/crew/*`, `/api/history/*`, `/api/artifacts/*`, or
`/api/hitl/*` namespaces. Those route families are retired compatibility references, not active contracts.
