# Database

## Overview

The backend uses Postgres as the primary persistence layer, with SQLAlchemy 2.x for ORM access and Alembic for schema
migrations.

Core database code lives under:

- `app/db/base.py`
- `app/db/session.py`
- `app/db/models/`
- `app/db/repositories/`

Compatibility helpers for older Mongo-backed flows still exist, but the intended steady state is repository-backed SQL
persistence.

## Configuration

Database configuration is environment-driven:

- `APP_ENV`
- `DATABASE_URL`
- `DATABASE_ECHO`
- `DATABASE_POOL_SIZE`
- `DATABASE_MAX_OVERFLOW`
- `REQUIRE_DATABASE`

Example:

```env
APP_ENV=development
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agency
DATABASE_ECHO=false
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10
```

In production, `DATABASE_URL` should be present. In isolated tests, the app can fall back to SQLite-backed test
configuration.

## Local Postgres Setup

Start the local database:

```bash
docker compose up -d postgres redis
```

Start the full local stack, including Langfuse observability services:

```bash
docker compose up --build
```

The grouped container assets for the local stack now live under:

- `docker/backend/`
- `docker/postgres/`
- `docker/redis/`

The local Postgres image includes pgvector and enables `vector` alongside `pgcrypto` and `uuid-ossp`. Durable memory
embeddings are stored on `memory_records.embedding_vector` for long-term semantic retrieval, with `embedding_json`
retained as a fallback/compatibility representation.

Apply schema migrations:

```bash
make migrate
```

Verify connectivity:

```bash
curl http://localhost:8000/health/db
```

## Local Snapshots

For a small private development database, you can export the Docker Compose Postgres database into a local snapshot and
import it later on the same machine or after moving it through a trusted private channel:

```bash
python scripts/db_snapshot.py export --database agency
```

On a Windows laptop after placing that snapshot under `database_exports/` and starting Postgres:

```powershell
docker compose up -d postgres
py scripts\db_snapshot.py import --database agency --yes
```

The helper uses `pg_dump` and `pg_restore` inside the Docker Compose Postgres container, so the host machine does not
need local Postgres client tools installed. Use `--database langfuse` to snapshot the Langfuse Postgres service, or
`--database all` for both configured Postgres databases. Use `--timestamped` if you also want an archive copy such as
`agency-20260514T120000Z.dump`.

These snapshots can contain credentials, API tokens, memory records, conversations, and other sensitive local data. Do
not commit raw snapshots; use sanitized fixtures or migrations for anything that must live in the repository.

## Tables

Current platform tables:

- `model_providers`
- `model_profiles`
- `agents`
- `tools`
- `workflows`
- `workflow_versions`
- `goals`
- `runtime_revisions`
- `executions`
- `execution_events`
- `execution_artifacts`
- `schedules`
- `credentials`
- `runtime_adapters`
- `mcp_servers`
- `a2a_agents`
- `approval_requests`
- `tool_invocations`
- `memory_sources`
- `memory_records`
- `prompt_templates`

JSON-heavy columns use Postgres `JSONB` in production.

## Relationships

Key relational links:

- `model_profiles.provider_id -> model_providers.id`
- `agents.model_profile_id -> model_profiles.id`
- `workflow_versions.workflow_id -> workflows.id`
- `goals.parent_goal_id -> goals.id`
- `executions.workflow_id -> workflows.id`
- `executions.goal_id -> goals.id`
- `executions.workflow_version_id -> workflow_versions.id`
- `executions.runtime_revision_id -> runtime_revisions.id`
- `execution_events.execution_id -> executions.id`
- `execution_artifacts.execution_id -> executions.id`
- `execution_artifacts.event_id -> execution_events.id`
- `approval_requests.execution_id -> executions.id`
- `approval_requests.event_id -> execution_events.id`
- `approval_requests.tool_id -> tools.id`
- `tool_invocations.execution_id -> executions.id`
- `tool_invocations.event_id -> execution_events.id`
- `tool_invocations.tool_id -> tools.id`
- `schedules.workflow_id -> workflows.id`

Important integrity rules include:

- indexed goal status, owner, parent, and updated-at lookups for supervisor scans
- indexed execution status, workflow, and created-at lookups
- indexed schedule enablement and next-fire timestamps
- indexed tool and workflow enablement
- unique `(execution_id, sequence)` on execution events

## Memory Storage

Persistent memory is stored in `memory_records`.

The table now serves:

- manual durable memory CRUD
- main-agent retrieved memory context
- `context_pack` compact conversation/workflow state for handoff and reuse
- `daily_summary` durable archives for conversations
- `run_summary` durable archives for non-main-agent executions

Important memory columns include:

- `scope`
- `created_by_user_id`
- `workspace_id`
- `conversation_id`
- `workflow_id`
- `agent_id`
- `memory_type`
- `status`
- `importance`
- `summary_date`
- `archived_window_start`
- `archived_window_end`
- `source_conversation_id`
- `source_execution_id`
- `supersedes_memory_id`

Important memory indexes include:

- `ix_memory_records_type_status`
- `ix_memory_records_source_conversation_summary_date`
- `ix_memory_records_summary_date_type`
- `ix_memory_records_agent_type`
- `ix_memory_records_workflow_type`
- `ix_memory_records_workspace_type`
- `ix_memory_records_user_type`

See [memory.md](./memory.md) for the application-level memory contract and retrieval behavior.

## Repository Pattern

Application code should go through repositories instead of embedding persistence logic in routes or runtimes.

Representative repositories:

- `SQLModelProviderRepository`
- `SQLModelProfileRepository`
- `SQLAgentRepository`
- `SQLToolRepository`
- `SQLWorkflowRepository`
- `SQLExecutionStore`
- `SQLScheduleRepository`
- `SQLCredentialRepository`
- `SQLRuntimeAdapterRepository`
- `SQLMCPServerRepository`
- `SQLA2AAgentRepository`

`SQLExecutionStore` owns the SQL mapping for execution rows, ordered execution events, artifacts, approvals, and runtime
metadata. Runtime-governance read snapshots are stored in `executions.metadata_json` and updated by
`save_execution()` on both insert and update paths; immutable audit history remains in `execution_events`.

The repository pattern keeps:

- route handlers thin
- runtime code persistence-agnostic
- transaction boundaries explicit
- tests isolated from production infrastructure

## Migration Commands

Create a revision:

```bash
./.venv/bin/python -m alembic revision --autogenerate -m "describe change"
```

Apply all migrations:

```bash
./.venv/bin/python -m alembic upgrade head
```

Show current revision:

```bash
./.venv/bin/python -m alembic current
```

Downgrade one revision:

```bash
./.venv/bin/python -m alembic downgrade -1
```

## Test Database Setup

The test suite does not require the production database.

Current test strategy:

- `sqlite+aiosqlite` for isolated DB tests
- temporary file-backed SQLite databases for migration and repository coverage
- mocked provider and runtime integrations for non-database dependencies

Useful DB-focused test command:

```bash
./.venv/bin/python -m unittest tests.test_database_foundation tests.test_postgres_schema tests.test_storage_migration
```

## Health Endpoints

- `GET /health`
- `GET /health/db`

`/health/db` returns success only when the SQLAlchemy engine can open a connection.

## Compatibility Notes

The project still contains a small number of compatibility persistence seams for older flows, but new work should not
add new persistence outside `app/db`.
