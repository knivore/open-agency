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

For normal local development, use the root startup shortcut:

```bash
make start
```

If you only need the backing services for a manual backend run, start Postgres and Redis directly:

```bash
docker compose up -d postgres redis
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

## Git-Based Local Snapshots

For a small private development database, you can export the Docker Compose Postgres database into a snapshot file that
can be committed to git and imported on another machine:

```bash
python scripts/db_snapshot.py export --database agency
git add database_exports/agency.dump database_exports/agency.json
git commit -m "Update local agency database snapshot"
```

On a Windows laptop after pulling the repository and starting Postgres:

```powershell
docker compose up -d postgres
py scripts\db_snapshot.py import --database agency --yes
```

The helper uses `pg_dump` and `pg_restore` inside the Docker Compose Postgres container, so the host machine does not
need local Postgres client tools installed. Use `--database langfuse` to snapshot the Langfuse Postgres service, or
`--database all` for both configured Postgres databases. Use `--timestamped` if you also want an archive copy such as
`agency-20260514T120000Z.dump`.

These snapshots can contain credentials, tokens, memory records, conversations, and other sensitive local data.
Commit them only to a private repository.

## Tables

Current platform tables:

- `model_providers`
- `model_profiles`
- `agents`
- `tools`
- `workflows`
- `workflow_versions`
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
- `executions.workflow_id -> workflows.id`
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

- indexed execution status, workflow, and created-at lookups
- indexed schedule enablement and next-fire timestamps
- indexed tool and workflow enablement
- unique `(execution_id, sequence)` on execution events

## Memory Storage

Persistent memory is stored in `memory_records`.

The table serves runtime-owned memory use cases:

- main-agent retrieved memory context
- `daily_summary` durable archives for conversations
- `run_summary` durable archives for non-main-agent executions

Important memory columns include:

- `scope`
- `created_by_user_id`
- `workspace_id`
- `conversation_id`
- `workflow_id`
- `agent_id`
- `memory_kind`
- `status`
- `importance`
- `summary_date`
- `archived_window_start`
- `archived_window_end`
- `source_conversation_id`
- `source_execution_id`
- `supersedes_memory_id`

Important memory indexes include:

- `ix_memory_records_kind_status`
- `ix_memory_records_source_conversation_summary_date`
- `ix_memory_records_summary_date_kind`
- `ix_memory_records_agent_kind`
- `ix_memory_records_workflow_kind`
- `ix_memory_records_workspace_kind`
- `ix_memory_records_user_kind`

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
