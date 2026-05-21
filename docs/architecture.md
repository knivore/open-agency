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
- main-agent chat planning and model-auth recovery are covered in [main-agent.md](./main-agent.md)
- migration trackers, TODO playbooks, and archive snapshots are intentionally excluded from the maintained documentation
  set

## app/domain

Purpose:

- defines the canonical backend model
- keeps workflow, execution, tool, schedule, and provider concepts independent from any one framework

Important models:

- `AgentDefinition`
- `ToolDefinition`
- `WorkflowDefinition`
- `Execution`
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

Executor imports should resolve to the `app/tools/executors/` package. Do not add a sibling `app/tools/executors.py`
compatibility file.

## Migrations

Schema migrations live in the top-level `alembic/` tree. Do not add a parallel empty `app/migrations` package unless it
contains an actual app-owned migration workflow.

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
