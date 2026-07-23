# Development

## Overview

New backend work should be added under `app/`. Avoid introducing new root-level architecture folders or new imports from
archived legacy paths.

For day-to-day local development, prefer the launcher:

```bash
./agency start
```

`./agency` is a thin wrapper around `./run.sh`, so `./run.sh start` and `make start` are equivalent. The launcher
creates `.env` from `.env.example`, creates `.venv`, installs backend dependencies, generates internal browser-runtime
identity when needed, builds the dedicated browser image, starts Docker services, applies migrations, runs headless
bootstrap checks for the default main agent, and starts the FastAPI backend. When a sibling
`../open-agency-fe` repo exists, it also writes the frontend `.env.local`, installs frontend dependencies, and starts
Next.js on `0.0.0.0`.

The launcher now also queries `GET /setup/status` after backend startup so local-first onboarding can stay beginner
friendly without replacing the existing developer stack. When onboarding is incomplete and `open-agency-fe` is present, the
recommended startup URL is `/setup` instead of dropping directly into the main product surfaces.

For normal first run, the intended path is the browser onboarding flow:

1. Open `/setup`.
2. Create the first local admin.
3. Configure one model profile.
4. Finish main-agent bootstrap.

The old `scripts/setup.py` flow still matters for headless bootstrap, CI-style provisioning, debugging, or operator
recovery, but it is not the preferred local onboarding path anymore.

When `open-agency-fe` is not present, the terminal fallback is:

```bash
python scripts/setup.py local-onboarding
make setup-local-onboarding
```

That command mirrors the `/setup` flow by creating the first local admin, configuring a model profile, and finishing
main-agent setup directly in the terminal. It now also offers a quick-setup path for the recommended Coder, Embedding,
and Evaluation agents so routine local setup no longer depends on a separate aggregate setup command.

Recommended validation commands:

- `make test`
- `make lint`
- `make check-architecture`
- `./.venv/bin/python -m unittest tests.test_documentation_consistency tests.test_legacy_import_check tests.test_architecture_validation`

Common launcher commands:

- `./agency doctor` to check prerequisites and ports
- `./agency bootstrap` to install backend/frontend dependencies without starting services
- `./agency start` to start everything available locally in the background
- `./agency logs` to stream backend and launcher-managed frontend logs
- `./agency restart` to stop, then start
- `./agency stop` to stop frontend, backend, and Docker services
- `./agency status` to inspect service health and generated frontend env
- `make secret-scan` to scan tracked files plus local `.env` files for high-signal secrets

Launcher file layout:

- `agency`, `run.sh`, and `run-windows.sh` are stable root wrappers for users, docs, Make targets, and installers.
- `scripts/launcher/run-unix.sh` contains the macOS/Linux launcher implementation.
- `scripts/launcher/run-windows.sh` contains the Git Bash/Windows launcher implementation.
- `scripts/launcher/common.sh` contains shared tunnel, setup-status, startup URL, and onboarding-sync helpers.
- `install/` stays limited to first-install/bootstrap scripts; runtime start/stop/log behavior belongs under `scripts/launcher/`.

Remote install entrypoints:

- `bash install/install-mac.sh`
- `bash install/install-linux.sh`
- `powershell -ExecutionPolicy Bypass -File .\install\install-windows.ps1`
- Add `--ngrok` or `--cloudflare` on macOS/Linux, or `-TunnelProvider ngrok|cloudflare` on Windows, when the first launch should expose a public webhook URL immediately.

The installers clone into `~/OpenAgency` by default and then hand off to the existing launcher so setup still converges on
the same browser or terminal onboarding paths. The macOS installer attempts to start Docker Desktop when it is installed
but not running. The Linux installer can install missing distro packages on apt/dnf/yum/pacman hosts, detects WSL, and
prints Docker Desktop WSL-integration guidance when Docker is not reachable. The Windows installer does the same preflight
in PowerShell before handing off to `.\run-windows.cmd start`. All installers run `./agency doctor` after bootstrap and
preserve the requested tunnel mode for first start.

On Windows, `run-windows.cmd` shows launcher output while retaining `%TEMP%\agency-run-windows.log`. When the sibling
`open-agency-fe` checkout is read-only (for example, outside a restricted Codex workspace root), startup automatically
uses the Compose `frontend` profile. The source bind is read-only and generated `.next` plus Linux `node_modules` state
live in named Docker volumes, so startup does not request elevation or modify directory ACLs.

Prerequisites:

- Docker Desktop
- Python 3.12 or newer
- Node.js/npm only when `AGENCY_FRONTEND_RUNTIME=native`; automatic/container mode supplies Node.js through Docker

When `open-agency-fe` is present locally and setup has already been completed, the default dev login is:

```text
Email: dev@example.com
Password: change-me
```

Useful startup overrides:

```bash
AGENCY_FRONTEND_ENABLED=false ./agency start
AGENCY_FE_DIR=/path/to/open-agency-fe ./agency start
AGENCY_OPEN_BROWSER=false ./agency start
AGENCY_PUBLIC_TUNNEL_PROVIDER=ngrok ./agency start
AGENCY_PUBLIC_TUNNEL_PROVIDER=cloudflare ./agency start
```

If `AGENCY_PUBLIC_TUNNEL_PROVIDER` is unset or `auto`, interactive startup can prompt for `local`, `ngrok`, or
`cloudflare`. Use tunnels only for local development. If you later deploy Agency to a real public host, disable local
tunneling and point providers at the deployed backend instead.

## Manual Local Setup

Use these steps when you need to debug the environment outside the launcher.

Create a local environment on macOS/Linux:

```bash
pyenv install 3.12.13
pyenv local 3.12.13
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
./agency start
```

Create a local environment on Windows with PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
\.\run-windows.cmd start
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
`chroma-hnswlib`, and `xattr`.

Patchright, Scrapling, and Chromium live only in the `browser-runtime` image; they are not installed into `.venv`.
Use `make check-browser-runtime` to validate the image contract. If an organization intercepts TLS, install its CA in
that image rather than disabling verification globally.

## Recommended Local Environment

Recommended minimum `.env` values:

```env
APP_ENV=development
AGENCY_ALLOWED_ORIGINS=
AGENCY_CORS_ALLOW_CREDENTIALS=true
AGENCY_INTERNAL_API_KEY=
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/agency
DATABASE_ECHO=false
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10
INTEGRATIONS_RUNTIME_ENABLED=false
EXECUTION_ISOLATION_ENABLED=false
WORKFLOW_SCHEDULER_ENABLED=true
WORKFLOW_SCHEDULER_INTERVAL_SECONDS=30
WORKFLOW_RESTART_ACTIVE_EXECUTIONS_ON_REVISION_CHANGE=false
GRAPH_PROJECTION_ENABLED=true
GRAPH_ENTITY_EXTRACTION_ENABLED=false
GRAPH_ENTITY_EXTRACTION_MIN_CONFIDENCE=0.7
GRAPH_DOCUMENT_PROJECTION_MAX_CHUNKS=500
AGENCY_GRAPH_CONTEXT_TOOLS_ENABLED=true
AGENCY_GRAPH_CONTEXT_QUERY_TIMEOUT_SECONDS=5
AGENCY_GRAPH_CONTEXT_RATE_LIMIT_WINDOW_SECONDS=60
AGENCY_GRAPH_CONTEXT_RATE_LIMIT_MAX_UNITS=5000
GRAPH_CONTEXT_AUTO_RETRIEVAL_ENABLED=false
GRAPH_CONTEXT_SUBAGENT_STEERING_ENABLED=false
GRAPH_CONTEXT_CODING_AGENT_RESUME_ENABLED=false
NEO4J_ENABLED=true
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=agency-neo4j-password
NEO4J_DATABASE=
RUNTIME_RECONCILER_ENABLED=false
RUNTIME_RECONCILER_INTERVAL_SECONDS=30
RUNTIME_IMAGE_RETENTION_COUNT=10
RUNTIME_CONTAINER_TTL_SECONDS=86400
REDIS_HOST=localhost
REDIS_PORT=6379
ENVIRONMENT=local
LOCAL_STORAGE_PATH=local_storage
```

`AGENCY_ALLOWED_ORIGINS` is a comma-separated CORS allowlist for browser clients such as `open-agency-fe`. Local
development automatically allows localhost frontend origins. For LAN, Tailscale, staging, or production, add the exact
frontend origin, for example `AGENCY_ALLOWED_ORIGINS=https://open-agency-fe.example.com`. Do not use `*` while credentials
are enabled.

`AGENCY_INTERNAL_API_KEY` is the scoped shared identity key used by explicit `open-agency-fe` BFF routes to delegate the
authenticated frontend user to `open-agency`. Set the same value in `open-agency-fe` as `AGENCY_FE_BFF_IDENTITY_KEY`. This is not
a general operator token for direct `agency` exposure.

Some compatibility flows still use additional environment variables for Redis, object storage, CrewAI, and
provider-specific credentials. Those are optional unless you are exercising those integrations.

## Local Services And Graph Stack

Start the default local backend stack:

```bash
docker compose up -d
```

Optional local services:

```bash
docker compose --profile onecli up -d onecli
```

Neo4j and the graph projector start with the default Compose stack. Project the local Neo4j schema manually when you
need an immediate one-shot rebuild outside the projector loop:

```bash
NEO4J_ENABLED=true ./.venv/bin/python -m app.cli graph-projection project-neo4j --ensure-schema
```

To inspect or rebuild the Neo4j projection from the durable outbox:

```bash
./.venv/bin/python -m app.cli graph-projection rebuild-neo4j --dry-run
NEO4J_ENABLED=true ./.venv/bin/python -m app.cli graph-projection rebuild-neo4j --clear --confirm-clear
NEO4J_ENABLED=true ./.venv/bin/python -m app.cli graph-projection parity --json
```

`--clear` deletes only projected Agency graph labels before replaying and requires `--confirm-clear`.

When `NEO4J_ENABLED=true`, the backend exposes read-only graph DTO endpoints for visualization clients:

- `GET /graph/read/status`
- `GET /graph/read/nodes/{node_id}`
- `GET /graph/read/nodes/{node_id}/neighborhood`
- `GET /graph/read/search?q=...`
- `GET /graph/read/workflows/{workflow_id}/lineage`

`GRAPH_DOCUMENT_PROJECTION_MAX_CHUNKS` caps the number of chunk-memory nodes linked from each projected `Document`
node. Set it to `0` to project all chunks.

`AGENCY_GRAPH_CONTEXT_TOOLS_ENABLED` controls whether read-only Agency Graph tools are included in built-in discovery
and seed data. The graph tool family includes `agency.graph.context`, `agency.graph.search`, `agency.graph.expand`,
`agency.graph.neighbors`, `agency.graph.path`, `agency.graph.summarize-subgraph`, and graph working-set tools for
collecting, summarizing, clearing, and persisting selected subgraphs as context packs.

`agency.graph.context` returns bounded, provenance-carrying context for anchors such as workflows, runs, agents, tasks,
memories, documents, or natural-language graph searches. It is intended for resume, debug, plan, audit, handoff,
steering, and root-cause workflows. If Neo4j is disabled, unavailable, timed out, or over budget, the tool returns a
structured fallback response instead of raw graph errors.

## Adding A New Agent

1. Add or update the canonical domain model in `app/domain/agents.py` if the shape changes.
2. Add or update the ORM model and repository behavior in `app/db/models/agents.py` and `app/db/repositories/agents.py`.
3. Add API schemas in `app/api/schemas/` if the request or response contract differs from the domain model.
4. Add or update route handlers in `app/api/routes/agents.py`.
5. Add tests for repository, API, and serialization behavior.

## Adding A New Tool

1. Define or extend the tool contract in `app/domain/tools.py`.
2. Choose stable identity fields before implementation: `id` for persistence/routing, callable-safe `name` for agents
   and runtimes, and readable `display_name` for frontend surfaces.
3. Register the tool through the builtin registry entrypoint in `app/tools/builtins.py`.
4. Update `app/tools/config/agency_tools.yaml` for human-owned builtin registry metadata. That now includes app-tool catalog entries as well as system-family gating metadata.
5. `app/tools/registry_config.py` is the shared YAML-backed registry config loader, so keep registry metadata in YAML rather than reintroducing duplicated catalog data in Python.
6. If the tool is a backend control-plane tool, keep the human-owned metadata in `app/tools/config/agency_tools.yaml`. `app/tools/system_catalog.py` wires family gating, `app/tools/system_specs.py` exposes YAML-backed declarative families such as workflow, management, connector, command, or execution tools, `app/tools/system_runtime_helpers.py` holds shared helper schemas, and `app/tools/system_runtime_families.py` holds runtime-heavy memory or graph declarations. Keep `app/services/agent_tools.py` focused on ids, policy gates, and shared schema annotation.
7. Add implementation code under `app/tools/implementations/`.
8. Add or update an executor under `app/tools/executors/` if the tool type is new.
9. Declare security metadata, approval requirements, validation rules, and parameter descriptions detailed enough for
   agents to select and call the tool without guessing.
10. Add tests for importability, validation, and execution behavior.

Builtin registry verification shortcuts:

- `make check-tool-registry`
- `python -m app.cli tool registry --json`

## Agency Graph Development

Agency Graph work spans durable Neo4j projection, graph read DTOs, agent tool contracts, and native-runtime
auto-retrieval. Keep these layers separate:

- `app/graph/*` owns projection, graph read helpers, presets, backfill, rebuild, and parity checks.
- `app/services/agency_graph_context.py` turns graph DTOs into bounded agent context.
- `app/tools/config/agency_tools.yaml` owns the graph tool metadata, and `app/tools/system_runtime_families.py` turns that metadata plus Python schema builders into runtime graph tool definitions.
- `app/services/agent_tools.py` applies system-tool ownership metadata and family gating.
- `app/tools/runtime/executor.py` dispatches graph tools and emits audit/observability events.
- `app/runtime/native/graph_context.py` owns opt-in runtime retrieval triggers.

Useful local graph commands:

```bash
./.venv/bin/python -m app.cli graph-projection backfill --json
./.venv/bin/python -m app.cli graph-projection backfill --domain source_intelligence_graph_hints --json
NEO4J_ENABLED=true ./.venv/bin/python -m app.cli graph-projection project-neo4j --ensure-schema
./.venv/bin/python -m app.cli graph-projection rebuild-neo4j --dry-run
NEO4J_ENABLED=true ./.venv/bin/python -m app.cli graph-projection parity --json
```

Focused validation for graph-agent changes:

```bash
./.venv/bin/python -m unittest tests.test_agency_graph_context_service
./.venv/bin/python -m unittest tests.test_graph_read_api tests.test_graph_projection tests.test_graph_parity
./.venv/bin/python -m unittest tests.test_tool_contract_runtime tests.test_main_agent_workflow_monitor
./.venv/bin/python -m unittest tests.test_coder_agent_setup tests.test_evaluation_agent_setup tests.test_embedding_agent_setup
```

Do not expose arbitrary Cypher to agents. Graph context tools must stay read-only, bounded, redacted, and policy
filtered. If a graph node references a durable memory record, agent output must respect memory visibility, sensitivity,
and workflow/agent/task/conversation exclusions.

Agency Graph deferred work and backlog:

- Run `NEO4J_ENABLED=true ./.venv/bin/python -m app.cli graph-projection rebuild-neo4j --clear --confirm-clear` only
  when it is acceptable to destructively clear and replay the live projected graph labels. Prefer non-destructive
  `rebuild-neo4j --batch-size ... --json` plus `parity --json` for routine validation.
- Keep backend work aligned with remaining frontend Agency Graph phases: backend projection pipeline, rich graph
  experience, query/search intelligence, governance/privacy/safety, performance/reliability, testing/release, and
  final rename/cleanup/docs removal.
- Add local seed fixtures that cover a successful workflow run, failed workflow run, stalled or repeated-progress
  sub-agent run, workflow-linked memories, uploaded documents with chunks/entities, context packs with decisions and
  next actions, tool failures, and approval requests.
- Keep tests independent from live local data. Live Neo4j parity checks are useful for development, but unit and
  integration tests should use deterministic repositories, seeded fixtures, and fake graph drivers.
- Extend graph read tests with canonical Agency Graph model cases across operational, memory, lineage, health, cost,
  governance, and security modes.
- Project future graph domains as they become structured in source data: first-class context packs when distinct from
  `Memory`, direct document-level entity mentions beyond chunk mentions, decisions, constraints, findings, open
  questions, next actions, and future governance records.
- Maintain idempotent upserts for every new graph record and keep graph-stream deltas consistent with durable Neo4j
  projection for direct document-entity and governance domains.
- Add OpenTelemetry spans for runtime graph tool events when an existing runtime-event exporter bridge supports them.
- Decide longer-term graph context behavior before expanding the tool surface: whether to consume frontend
  event-derived fallback graphs, whether to include full memory content or summaries by default, whether to persist
  graph-derived context packs automatically, where working sets should live, whether deterministic synthesis remains
  default if optional LLM summarization is added, how graph traversal permissions should be represented, how file
  paths/artifacts/code changes should be projected for coding-agent resume flows, whether execution event sequences
  need richer root-cause projection, whether graph search should combine Neo4j text search with pgvector memory search,
  which agents should receive graph context tools by default, and what UI affordance should send selected graph nodes
  or subgraphs to the main agent.

## Adding A New Workflow

1. Extend the canonical workflow model in `app/domain/workflows.py` if needed.
2. Persist workflow and version changes through `app/db/models/workflows.py` and repositories.
3. Expose API changes through `app/api/routes/workflows.py`.
4. Ensure runtimes can consume the resulting workflow definition.
5. Add tests for CRUD, versioning, and runtime execution.

Validation note:

- `POST /workflows/validate` expects a `WorkflowDefinition`, not the full operator-facing payload returned by
  `GET /workflows/{workflow_id}`.
- Strip response-only sections such as `monitoring` and `runtime_governance` before round-tripping a stored workflow
  definition back into the validator.

## Adding A New Runtime Adapter

1. Create the adapter under `app/runtime/adapters/`.
2. Map canonical domain models into adapter-specific execution objects there.
3. Emit canonical execution events rather than adapter-native event shapes.
4. Register or expose adapter availability through runtime adapter records.
5. Add tests for adapter selection, availability, and execution behavior.

Do not import framework-specific types into `app/domain` or route handlers.

## Adding A New Model Provider

1. Add provider and profile support in `app/llm/`.
2. Persist provider and profile records via `app/db/models/` and repositories.
3. Expose provider configuration through `app/api/routes/models.py`.
4. Mock provider behavior in tests. Do not call live cloud APIs from the test suite.

## Documentation Expectations

When the architecture changes:

- update [README.md](../README.md)
- update the relevant file in `docs/`
- add or revise an ADR in `docs/adr/`, for example `docs/adr/0001-hybrid-tool-registry.md`, if the change is architectural
- remove or rewrite outdated docs rather than leaving conflicting guidance in place

## Computer Use Development

Computer Use support in this repo is MCP-backed and host-aware.

Current built-in server ids:

- `computer-use-macos`
- `computer-use-windows`

Current default external commands:

- macOS: `uvx macos-mcp`
- Windows: `uvx windows-mcp`

Development workflow:

1. Install or otherwise make the target external MCP command available on the host.
2. Override the default command or args with:
    - `COMPUTER_USE_MACOS_MCP_COMMAND`
    - `COMPUTER_USE_MACOS_MCP_ARGS`
    - `COMPUTER_USE_WINDOWS_MCP_COMMAND`
    - `COMPUTER_USE_WINDOWS_MCP_ARGS`
3. Start the backend.
4. Let startup seed the built-in `MCPServerDefinition` rows.
5. Sync discovery through startup auto-sync or `POST /mcp-servers/discover`.
6. Verify the normalized `mcp:computer-use-...:*` tools exist in the tool catalog.

Important boundaries:

- the main agent should depend on Agency-normalized Computer Use tool names, not raw upstream names
- read-only desktop inspection should stay separate from mutating tools that require approval
- Computer Use is not a replacement for the browser tool family

Use browser tools when the task is clearly webpage-centric and DOM/browser semantics matter.
Use Computer Use when the task is desktop-native, cross-application, OS-dialog-driven, or otherwise not well represented
as a browser session.
