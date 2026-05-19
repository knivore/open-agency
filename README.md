# agency

`agency` is the backend for an app-centric agentic workflow platform. It exposes APIs for defining agents, tools,
workflows, schedules, runtime adapters, model providers, and executions, then runs those executions through a native
runtime with compatibility support for framework adapters.

The canonical architecture now lives under `app/`, with Postgres, SQLAlchemy, and Alembic as the primary persistence
stack.

## Project Purpose

This backend provides:

- catalog APIs for agents, tools, workflows, model providers, model profiles, runtime adapters, MCP servers, and A2A
  agents
- execution APIs for starting, observing, approving, and inspecting workflow runs
- schedule-driven execution and execution event persistence
- adapter-based runtime integration for native execution and CrewAI compatibility
- a security model for tool execution, approval, sandboxing, and protocol boundaries

Tool definitions use a split identity contract:

- `id` is the stable persistence and routing identity, for example `agency.memory.delete`
- `name` is the callable-safe agent/runtime name, for example `delete_memory`
- `display_name` is the human label for UI surfaces, for example `Delete Memory`

Agents, model payloads, CLI discovery, and runtime execution should use callable `name` values. Frontend surfaces should
render `display_name` and avoid exposing raw implementation details unless they are in an explicit developer/debug view.

## Architecture Overview

The backend is structured around these layers:

- `app/api`: FastAPI app factory, routes, dependency wiring, and API schemas
- `app/domain`: canonical Pydantic models independent of any execution framework
- `app/db`: SQLAlchemy base, async session management, ORM models, and repositories
- `app/services`: thin service layer used by routes and orchestration code
- `app/runtime`: native runtime, execution control plane, and framework adapters
- `app/tools`: tool definitions, registry, validation, executors, and implementations
- `app/llm`: model provider and profile resolution
- `app/scheduler`: schedule loading, trigger calculation, and queued workflow execution
- `app/protocols`: MCP and A2A integration boundaries
- `app/observability`: execution timeline and telemetry aggregation

Core reference docs:

- [Architecture](./docs/architecture.md)
- [Database](./docs/database.md)
- [Frontend API](./docs/frontend-api.md)
- [Main Agent](./docs/main-agent.md)
- [Model Profiles](./docs/model-profiles.md)
- [Runtime](./docs/runtime.md)
- [Runtime Adapters](./docs/runtime-adapters.md)
- [Computer Use](./docs/computer-use.md)
- [MCP Integration](./docs/mcp-integration.md)
- [A2A Integration](./docs/a2a-integration.md)
- [Tools](./docs/tools.md)
- [Voice and Transcription](./docs/voice.md)
- [Tool Contracts](./docs/tool-contracts.md)
- [Coding Agent](./docs/coding-agent.md)
- [Evaluation Agent](./docs/evaluation-agent.md)
- [Development](./docs/development.md)
- [Testing](./docs/testing.md)

Documentation state:

- the active source of truth is `README.md` plus the current developer docs under `docs/`
- TODO playbooks, archive snapshots, migration reports, and one-off implementation notes are intentionally excluded from
  the maintained docs set
- keep future docs focused on setup, architecture, API contracts, runtime behavior, persistence, tools/integrations, and
  testing

## Computer Use MCP Backends

Built-in Computer Use MCP backend seeding, environment overrides, and operational requirements are documented
in [docs/computer-use.md](./docs/computer-use.md).

## Local Setup

Create a local environment on macOS/Linux:

```bash
pyenv install 3.12.13
pyenv local 3.12.13
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install
cp .env.example .env
```

Create a local environment on Windows with PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install
Copy-Item .env.example .env
```

If PowerShell blocks virtual environment activation, allow local scripts for the current user, then activate again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\.venv\Scripts\Activate.ps1
```

If the `py -3.12` launcher cannot find Python 3.12, install Python 3.12 from
[python.org](https://www.python.org/downloads/windows/) or with `winget`:

```powershell
winget install Python.Python.3.12
```

The requirements file skips Unix-only or unused native packages on Windows, including `uvloop`, `chromadb`,
`chroma-hnswlib`, and `xattr`. Durable memory uses the Postgres/pgvector stack documented below.

If Playwright browser installation fails with `unable to verify the first certificate`, your network is probably
intercepting TLS certificates. Prefer configuring Node with your organization's root CA:

```powershell
$env:NODE_EXTRA_CA_CERTS = "C:\path\to\your-company-root-ca.pem"
python -m playwright install
Remove-Item Env:NODE_EXTRA_CA_CERTS
```

For a one-time local browser download only, you can bypass Node TLS verification temporarily:

```powershell
$env:NODE_TLS_REJECT_UNAUTHORIZED = "0"
python -m playwright install
Remove-Item Env:NODE_TLS_REJECT_UNAUTHORIZED
```

Recommended minimum `.env` values:

```env
APP_ENV=development
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agency
DATABASE_ECHO=false
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10
INTEGRATIONS_RUNTIME_ENABLED=false
EXECUTION_ISOLATION_ENABLED=false
WORKFLOW_SCHEDULER_ENABLED=true
WORKFLOW_SCHEDULER_INTERVAL_SECONDS=30
WORKFLOW_RESTART_ACTIVE_EXECUTIONS_ON_REVISION_CHANGE=false
RUNTIME_RECONCILER_ENABLED=false
RUNTIME_RECONCILER_INTERVAL_SECONDS=30
RUNTIME_IMAGE_RETENTION_COUNT=10
RUNTIME_CONTAINER_TTL_SECONDS=86400
REDIS_HOST=localhost
REDIS_PORT=6379
ENVIRONMENT=local
LOCAL_STORAGE_PATH=local_storage
```

Some compatibility flows still use additional environment variables for Redis, object storage, CrewAI, and
provider-specific credentials. Those are optional unless you are exercising those integrations.

## Start The Repo

The normal local loop is:

```bash
make start
```

That is a shortcut for:

```bash
./run.sh start
```

This starts Postgres, Redis, and supporting containers, writes `../agency-fe/.env.local` LAN proxy settings, builds the
backend image for isolated workers, runs Alembic migrations, runs agent setup, starts the FastAPI backend on the host,
and starts the frontend on `0.0.0.0`.

Use these one-shot commands for the common lifecycle:

```bash
make start
make stop
make status
```

Equivalent script commands:

```bash
./run.sh start
./run.sh stop
./run.sh status
```

`make start` creates or updates the main agent, Coder, Embedding, and Evaluation agents as part of startup.

The Makefile is a convenience layer for common repo tasks. The launch scripts remain the source of truth for starting
and stopping the local stack.

Windows users should run the Bash launcher from Git Bash or WSL, not plain PowerShell:

```bash
./run-windows.sh start
./run-windows.sh stop
./run-windows.sh status
```

From PowerShell or Command Prompt, use the wrapper:

```bat
.\run-windows.cmd start
.\run-windows.cmd stop
.\run-windows.cmd status
```

If you only want dependencies in Docker and a manually started host backend:

```bash
docker compose up -d postgres redis
make migrate
make dev
```

Useful Makefile commands:

```bash
make start
make stop
make status
make setup-agents
make migrate
make test
make eval
```

## Runtime Behavior

Workflow schedules are driven by Agency's internal scheduler runner, not by OS cron. Set
`WORKFLOW_SCHEDULER_ENABLED=true` on the backend process that should fire due schedules. On startup, that process starts
a background loop that calls the scheduler every `WORKFLOW_SCHEDULER_INTERVAL_SECONDS` seconds, creates executions for
due schedules, and queues those executions through the runtime control plane. In multi-replica deployments, enable this
on only one process unless the deployment is using the database-backed schedule fire claims table. The claim table
enforces one winner per `(schedule_id, scheduled_fire_at)` so multiple scheduler runners cannot create duplicate
executions for the same scheduled fire time.

Scheduled workflows are modeled as recurring execution requests, not as a single permanently running container. Each
scheduled fire creates an execution tagged with `execution_lifecycle.run_mode=scheduled`; manual/API runs are tagged as
`one_time`. By default, both modes persist final execution state, output, events, and artifacts to the Agency database,
then remove the finished worker container after reconciliation. Future always-on workflows can opt out through workflow
metadata `execution_lifecycle.terminate_container_on_completion=false`.

Workflow revision replacement is opt-in. By default, publishing a new workflow revision affects future executions only;
already-running executions continue with the workflow definition they started with. Set
`WORKFLOW_RESTART_ACTIVE_EXECUTIONS_ON_REVISION_CHANGE=true`, or pass `restart_active_executions: true` when publishing,
to create replacement executions with the same input payload and cancel/remove active containers for the superseded
revision.

Runtime-isolation-specific settings:

- `EXECUTION_RUNTIME_DATABASE_URL`
  Use this when the worker container must connect to a different runtime-visible database address than the host process.
  This is mainly useful for local SQLite-backed Docker tests or unusual mount-based runtime setups.
- `EXECUTION_CONTAINER_EXTRA_MOUNTS`
  JSON list of extra mounts passed to isolated worker containers. Example:

```json
[
  {
    "source": "/host/path",
    "target": "/runtime/path",
    "read_only": false
  }
]
```

## Database Snapshot

Start the local backend dependencies:

```bash
docker compose up -d postgres redis
```

Export the local app database into a git-trackable snapshot:

```bash
python scripts/db_snapshot.py export --database agency
git add database_exports/agency.dump database_exports/agency.json
```

After pulling the repository on Windows, import that snapshot into the local Docker Postgres service:

```powershell
docker compose up -d postgres
py scripts\db_snapshot.py import --database agency --yes
```

Only commit database snapshots to a private repository; they can include credentials, tokens, memory records, and local
conversation data.

## Runtime Modes

Use `make start` for day-to-day work. The underlying host-backend mode runs the main FastAPI/Codex process on the host
and keeps Postgres, Redis, and isolated execution workers in Docker. Workflow/tool execution remains isolated through
Docker workers by default with:

```env
AGENCY_BACKEND_RUN_MODE=host
INTEGRATIONS_RUNTIME_ENABLED=true
EXECUTION_ISOLATION_ENABLED=true
EXECUTION_RUNTIME_DATABASE_URL=postgresql://postgres:postgres@postgres:5432/agency
EXECUTION_CONTAINER_NETWORK=agency_default
CODEX_CLI_CWD=/path/to/agency
EXECUTION_CODEX_CLI_CWD=/app
```

Use `CODEX_CLI_CWD` for the host-side main-agent Codex working directory. Use `EXECUTION_CODEX_CLI_CWD` for worker
containers; it should stay container-visible, usually `/app`.

The local backend runs on the host and isolated execution workers use the built backend image. Normal backend code
changes are picked up by Uvicorn reload.

On Windows, run `./run-windows.sh start` from Git Bash or WSL.

## LAN Access

For mobile/LAN access, run the start command from this backend repo. It writes the frontend proxy settings into
`../agency-fe/.env.local` and starts the frontend on `0.0.0.0`:

```bash
./run.sh start
```

On Windows Git Bash:

```bash
./run-windows.sh start
```

Then open the printed frontend URL on your phone, for example `http://192.168.68.62:3000`. If automatic LAN IP detection
chooses the wrong adapter, provide it explicitly:

```bash
LAN_HOST=192.168.68.62 ./run.sh start
```

The script writes `../agency-fe/.env.local` with backend proxy and local development settings.

Stop the LAN frontend and backend stack with the same script:

```bash
./run.sh stop
```

Check whether the backend, frontend port, and generated frontend LAN environment are healthy:

```bash
./run.sh status
```

On Windows Git Bash, use `./run-windows.sh stop` and `./run-windows.sh status`.

## Maintenance Commands

Rebuild the Docker backend after dependency, Dockerfile, or Compose changes:

```bash
docker compose up --build --force-recreate backend
```

Then, if the backend does not automatically migrate cleanly, run:

```bash
docker compose exec backend python -m alembic upgrade head
```

Or:

```bash
docker compose down -v
make start
```

Apply migrations:

```bash
make migrate
```

Windows PowerShell equivalent:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Equivalent Alembic commands:

```bash
./.venv/bin/python -m alembic upgrade head
./.venv/bin/python -m alembic current
./.venv/bin/python -m alembic downgrade -1
```

Windows PowerShell equivalents:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic downgrade -1
```

Database details, schema, and repository guidance are documented in [docs/database.md](./docs/database.md).

## First-Run Main-Agent Setup

First-run main-agent bootstrap, runtime resolution, and operator commands are documented
in [docs/main-agent.md](./docs/main-agent.md).

Quick commands:

```bash
make setup-agents
make sync-main-agent-prompt
make check-main-agent
make eval
```

`make setup-agents` is the normal first-run path. It provisions the main agent plus the Coder, Embedding, and Evaluation
agents. The evaluation agent is a read-only semantic judge for eval runs. It should use a distinct model profile from
the main, Coder, and Embedding agents, and its setup is documented in
[docs/evaluation-agent.md](./docs/evaluation-agent.md).
The default deterministic eval suite runs with `make eval`; CI-safe case definitions live under `evals/cases`.

## Main Agent Conversations

The backend-native conversation surface, LLM-first architecture, and model-auth recovery contract are documented in
[docs/main-agent.md](./docs/main-agent.md).
Native workflow agents can also share durable memory through the same `memory_records` table. Enable shared memory with
workflow metadata such as `{"shared_memory": {"enabled": true}}` or by setting an agent's `memory.enabled=true` with a
non-`execution` scope. Prefer `workflow` scope for memory shared by agents in one workflow and `workspace` scope for
cross-workflow project memory. Operators can use `GET`/`PATCH /workflows/{workflow_id}/shared-memory` for this without
submitting a full workflow update.
Document uploads can be ingested through `POST /documents/ingest`; the backend stores the source file, extracts text,
chunks it into `archive` memory records, embeds the chunks, and retrieves them later through the pgvector-backed memory
search path.

Canonical routes:

- `POST /conversations`
- `GET /conversations`
- `PATCH /conversations/main-agent-profile`
- `GET /conversations/{conversation_id}`
- `PATCH /conversations/{conversation_id}`
- `GET /conversations/{conversation_id}/messages`
- `POST /conversations/{conversation_id}/messages`
- `GET /conversations/{conversation_id}/stream`
- `GET /conversations/main-agent-profile`

Plain user text is planned by the configured main-agent LLM first. The backend exposes policy-visible tools and
workflows to that model, then validates tool calls and approval-gated side effects. Explicit structured payloads from
the UI, such as `content.execution_request` or `content.workflow_update_proposal`, may still execute through deterministic
service paths because they are already app commands.

If the configured model needs re-authentication, conversation APIs should return `200 OK` with an assistant message and
`assistant_message.metadata.model_auth`, not a generic 500. Frontends should render a re-auth action when
`model_auth.reauthorization_required` is true and call the provided `model_auth.auth_endpoint`.

## Running The Backend

Start the FastAPI backend:

```bash
make dev
```

Equivalent command:

```bash
SSL_CERT_FILE=certs/local_cloudflare.cert ./.venv/bin/python -m uvicorn app:app --reload
```

Windows PowerShell equivalent:

```powershell
$env:SSL_CERT_FILE = "certs/local_cloudflare.cert"
.\.venv\Scripts\python.exe -m uvicorn app:app --reload
```

If you are running the backend directly on Windows instead of inside Docker, start Postgres and Redis first:

```powershell
docker compose up -d postgres redis
.\.venv\Scripts\python.exe -m alembic upgrade head
$env:SSL_CERT_FILE = "certs/local_cloudflare.cert"
.\.venv\Scripts\python.exe -m uvicorn app:app --reload
```

Once the server is running, you can use the built-in API docs directly without the frontend:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- OpenAPI schema: `http://localhost:8000/openapi.json`

Swagger UI is the easiest way to exercise endpoints manually because it lets you inspect request and response schemas
and send requests from the browser.

The canonical app factory is `app.api.main:create_app()`. The root [app.py](./app.py) file is intentionally a thin
entrypoint.

Container definitions are grouped under:

- [docker/backend/Dockerfile](./docker/backend/Dockerfile)
- [docker/postgres/Dockerfile](./docker/postgres/Dockerfile)
- [docker/postgres/initdb/001-extensions.sql](./docker/postgres/initdb/001-extensions.sql)
- [docker/redis/Dockerfile](./docker/redis/Dockerfile)
- [docker/redis/redis.conf](./docker/redis/redis.conf)
- [docker/langfuse/README.md](./docker/langfuse/README.md)

The Agency Postgres image includes pgvector. Durable memory embeddings are persisted to
`memory_records.embedding_vector` for long-term semantic retrieval; `embedding_json` remains available as a fallback and
compatibility copy.

The local compose stack also provisions a self-hosted Langfuse stack using the official upstream images:

- `langfuse-web`
- `langfuse-worker`
- `langfuse-postgres`
- `langfuse-redis`
- `langfuse-clickhouse`
- `langfuse-minio`

Those services are intentionally separate from the app's own Postgres and Redis so observability infrastructure does not
share the same database schema or cache instance as workflow execution.

The backend container startup now does three things automatically:

1. waits for Postgres and Redis
2. runs `alembic upgrade head`
3. starts `uvicorn app:app`

Postgres itself still runs as a separate service in `docker-compose.yml`. That is intentional: Dockerfiles build images,
while Compose orchestrates multiple linked services. The Postgres service now also has its own grouped image under
`docker/postgres/`, so all containerized components live under the same `docker/` tree.

Why there is an `entrypoint.sh` as well as `run.sh` / `run-windows.sh`:

- [run.sh](./run.sh)
  and [run-windows.sh](./run-windows.sh) run on the host machine.
  Their job is to start dependencies and supporting services in Docker, run migrations and agent setup, then run the
  FastAPI main-agent process and frontend on the host.
- [docker/backend/entrypoint.sh](./docker/backend/entrypoint.sh) runs
  inside the backend container. Its job is to wait for Postgres and Redis, apply Alembic migrations, and then start the
  API.

That separation is intentional. If the backend container restarts on its own, or if someone starts the service directly
with Docker Compose instead of the host scripts, the container still needs a correct self-contained startup sequence.

## Langfuse Observability Stack

The full Docker Compose stack now includes self-hosted Langfuse.

Useful local endpoints:

- backend API: `http://localhost:8000`
- Langfuse UI/API: `http://localhost:3001`
- Langfuse MinIO S3 endpoint: `http://localhost:9090`
- Langfuse MinIO console: `http://localhost:9091`

The backend container is also wired with:

- `LANGFUSE_HOST=http://langfuse-web:3001`
- `LANGFUSE_PUBLIC_KEY`
- `LANGFUSE_SECRET_KEY`
- `OBSERVABILITY_EXPORTERS`

By default the backend still uses `OBSERVABILITY_EXPORTERS=jsonl`. To include Langfuse in local exporter selection,
update `.env` to:

```env
OBSERVABILITY_EXPORTERS=jsonl,langfuse
```

This Compose work provisions the Langfuse infrastructure and backend connectivity settings. Actual event export still
depends on the app-side Langfuse exporter configuration and valid Langfuse project keys.

## Running Tests

Run the full suite:

```bash
make test
```

Windows PowerShell equivalent:

```powershell
.\.venv\Scripts\python.exe -m unittest
```

Run architecture checks:

```bash
make check-architecture
```

Windows PowerShell equivalent:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_legacy_import_check tests.test_architecture_validation
```

Run compile and architecture validation:

```bash
make lint
```

Windows PowerShell equivalent:

```powershell
.\.venv\Scripts\python.exe -m compileall app tests
.\.venv\Scripts\python.exe -m unittest tests.test_legacy_import_check tests.test_architecture_validation
```

Database-focused tests:

```bash
./.venv/bin/python -m unittest tests.test_database_foundation tests.test_postgres_schema tests.test_storage_migration
```

Windows PowerShell equivalent:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_database_foundation tests.test_postgres_schema tests.test_storage_migration
```

Docker-backed isolated runtime tests:

```bash
ENABLE_DOCKER_INTEGRATION_TESTS=1 ./.venv/bin/python -m unittest tests.test_docker_worker_integration
```

Windows PowerShell equivalent:

```powershell
$env:ENABLE_DOCKER_INTEGRATION_TESTS = "1"
.\.venv\Scripts\python.exe -m unittest tests.test_docker_worker_integration
```

## Isolated Runtime Operations

The isolated execution runtime now supports:

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
[tests/test_docker_worker_integration.py](./tests/test_docker_worker_integration.py).
