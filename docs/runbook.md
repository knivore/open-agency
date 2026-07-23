# Platform Runbook

This runbook collects stable operator steps for the backend/runtime stack. Developer design details live in the focused
guides under `docs/`; curl-heavy Persona Factory examples live in `docs/persona-factory-cli.md`.

## Local Bring-Up

Start the local stack:

```bash
./agency start
```

Check service health and generated frontend environment:

```bash
./agency status
```

Stop the stack:

```bash
./agency stop
```

The launcher starts backend/runtime services, including the dedicated `browser-runtime`, and, when `../open-agency-fe`
exists, starts the frontend and writes `../open-agency-fe/.env.local`. Set `AGENCY_BACKEND_ONLY=1` for backend-only startup.
On first launch it generates `BROWSER_RUNTIME_SIGNING_SECRET` in `.env`; this is an internal capability-signing key,
not a credential an operator needs to obtain or rotate for routine local use.

For local development and the Codex-first chat path, this launcher is the preferred entrypoint. It starts Postgres,
Redis, OneCLI, and Langfuse with Docker Compose, creates the local Python environment when needed, builds the backend
image for isolated worker containers, syncs host Codex OAuth into the Docker Codex volume for worker use, applies
Alembic migrations from the host, runs headless bootstrap checks for the default main agent, and starts `uvicorn` on the host.

When the frontend is present, first-run local onboarding should normally happen in the browser at `/setup`: create the
local admin, connect a model profile, and finish the main-agent bootstrap there. Keep `scripts/setup.py` for
headless/operator recovery or provisioning flows rather than as the default user path.

If the frontend repo is absent and you are bringing up a backend-only local install, use:

```bash
python scripts/setup.py local-onboarding
```

That terminal command replicates the same first-run setup sequence without requiring `open-agency-fe`.

Launcher shortcuts:

```bash
./agency doctor
./agency bootstrap
./agency start
./agency logs
./agency restart
./agency stop
./agency status
```

Useful startup overrides:

- `AGENCY_FRONTEND_ENABLED=false` to force backend-only startup even when `../open-agency-fe` exists
- `AGENCY_FE_DIR=/path/to/open-agency-fe` to use a frontend repo in a different location
- `./agency start` starts with the saved setup preference, or otherwise tries a public tunnel by default
- `./agency logs` streams the backend and launcher-managed frontend logs after background startup
- `./agency start -local` forces local-only startup with no public tunnel
- `./agency start -cloudflare` starts the same local stack plus a Cloudflare Tunnel
- `./agency start -ngrok` starts the same local stack plus an ngrok tunnel
- `./agency start -cloudflare --domain agency.example.com` uses a reserved provider hostname for that launch
- `AGENCY_PUBLIC_TUNNEL_PROVIDER=none|ngrok|cloudflare|auto` remains available for automation or shell aliases

Cloudflare is usually the better fit when the laptop or network already runs through Cloudflare Zero Trust. Ngrok is
better when you want a quick public URL and outbound TLS interception is not in the way.

## Database And Snapshot Operations

Start only the local backend dependencies:

```bash
docker compose up -d postgres redis
```

Start the optional OneCLI credential gateway for local migration work:

```bash
docker compose --profile onecli up -d onecli
```

The OneCLI dashboard is available at `http://127.0.0.1:10254` and the gateway at `http://127.0.0.1:10255`. See
[`docs/onecli.md`](./onecli.md) before exposing either service beyond localhost.

Export the local app database into a local snapshot:

```bash
python scripts/db_snapshot.py export --database agency
```

If you need the snapshot on another machine, move it outside git and place it under `database_exports/` before
importing. On Windows, import it into the local Docker Postgres service after placing it there:

```powershell
docker compose up -d postgres
py scripts\db_snapshot.py import --database agency --yes
```

Do not commit raw database snapshots. They can include credentials, tokens, memory records, and local conversation
data. Use sanitized fixtures for repository-shared test data.

## Runtime Bring-Up Modes

Start the full local stack in containers:

```bash
docker compose up --build
```

The local backend container bind-mounts `app/`, `alembic/`, `scripts/`, `docs/`, `app.py`, and `alembic.ini`, then
starts Uvicorn with reload enabled. Rebuild the backend only when Docker image inputs change:

```bash
docker compose up --build --force-recreate backend
```

The browser runtime health endpoint is local-only at `http://127.0.0.1:8010/health`. Use the launcher at least once
before invoking Compose directly so `.env` contains the generated signing secret. Browser policy defaults and ceilings
remain editable in `.env`; per-open agent preferences are clamped to those limits.

To run the backend container without auto-reload:

```bash
BACKEND_RELOAD=false docker compose up --build backend
```

If you only need dependencies and will start the backend manually:

```bash
docker compose up -d postgres redis
make dev
```

On Windows, make sure Docker Desktop is running first, then start the same stack from Git Bash or WSL:

```bash
./run-windows.sh start
```

For a fresh Linux or WSL install, use the Linux installer entrypoint:

```bash
AGENCY_BACKEND_COMMIT=<full-backend-commit> \
AGENCY_FRONTEND_COMMIT=<full-frontend-commit> \
  bash install/install-linux.sh
```

The Linux installer can install missing Git, curl, Python, Node/npm, tar, and unzip packages on common apt/dnf/yum/pacman
distributions. Both repository revisions must be full immutable commit IDs; use `--backend-only` when no frontend commit
is needed. On WSL, Docker still needs to come from Docker Desktop for Windows with WSL integration enabled.

## Tunnels And Public Webhook URLs

The public tunnel exposes the Agency backend as a whole, so Discord, Telegram, WhatsApp, Slack, and other chat
adapters can all use the same public backend host with their own adapter routes.

Public tunnel behavior:

- `./agency start` or `./run-windows.sh start` first applies explicit CLI tunnel arguments, then the browser-saved preference, then tunnel-first auto detection.
- Without a saved preference or CLI override, auto detection chooses a configured Cloudflare installation, then ngrok, then Cloudflare quick tunnel.
- If the selected tunnel provider cannot run, startup continues local-only and prints the provider log path.
- `./agency start -local` or `./run-windows.sh start -local` forces local-only startup
- `./agency start -ngrok` or `./run-windows.sh start -ngrok` starts an ngrok tunnel
- `./agency start -cloudflare` or `./run-windows.sh start -cloudflare` starts a Cloudflare Tunnel
- `./agency start -cloudflare --domain agency.example.com` or `./run-windows.sh start -cloudflare --domain agency.example.com` uses a reserved provider hostname for that launch
- The authenticated `/setup` tunnel control saves provider and custom domain to `.agency/tunnel-preference.json`.
- A browser-saved preference is the normal default on later starts; explicit CLI tunnel arguments override it for the current launch.
- `AGENCY_IGNORE_SAVED_TUNNEL_PREFERENCE=true` temporarily bypasses the saved browser preference.
- `AGENCY_PUBLIC_TUNNEL_PROVIDER=none` forces local-only startup
- `AGENCY_PUBLIC_TUNNEL_PROVIDER=ngrok` starts an ngrok tunnel
- `AGENCY_PUBLIC_TUNNEL_PROVIDER=cloudflare` starts a Cloudflare Tunnel
- `AGENCY_PUBLIC_TUNNEL_PROVIDER=auto` performs tunnel-first host capability detection
- `AGENCY_TUNNEL_CUSTOM_DOMAIN=agency.example.com` replaces the provider-assigned public URL
- `AGENCY_NGROK_AUTHTOKEN` supplies the ngrok token non-interactively
- `AGENCY_CLOUDFLARE_TUNNEL_TOKEN` enables managed Cloudflare Tunnel mode
- `AGENCY_CLOUDFLARE_TUNNEL_PUBLIC_URL` remains available for managed tunnels without a browser-saved custom domain

For ngrok, a custom domain must already be reserved in the ngrok account and configured through DNS; ngrok documents
custom domains as a paid capability. For Cloudflare, a custom domain requires a managed tunnel token and a published
application route for that hostname. Agency stores the hostname preference, but provider tokens stay in the existing
provider configuration rather than the browser-readable preference file.

If you deploy Agency to a real public host later, disable local tunneling and point providers at the deployed backend
instead.

## Persona Factory Operations

Persona Factory converts selected memory records into governed, reviewable Persona packages. The backend remains usable
without `open-agency-fe`.

Use these default modes:

- `llm`: LLM distillers are the default main extraction path.
- `deterministic`: local deterministic extraction, available as a fallback and low-cost baseline.
- `hybrid`: deterministic and LLM extraction both run, then candidates are merged for review.

Key operational settings:

- `PERSONA_FACTORY_DEFAULT_DISTILLATION_MODE`, default `llm`
- `PERSONA_FACTORY_DEFAULT_LLM_MODEL_SOURCE`, default `main_agent`
- `PERSONA_FACTORY_LLM_DISTILLATION_ENABLED`, default `true`
- `PERSONA_FACTORY_HYBRID_DISTILLATION_ENABLED`, default `true`
- `PERSONA_FACTORY_MAX_DOCUMENTS_PER_RUN`, default `25`
- `PERSONA_FACTORY_MAX_SOURCE_MEMORIES_PER_RUN`, default `250`
- `PERSONA_FACTORY_MAX_SOURCE_CHARACTERS_PER_RUN`, default `300000`
- `PERSONA_FACTORY_LLM_MAX_SOURCE_MEMORIES_PER_RUN`, default `100`
- `PERSONA_FACTORY_LLM_MAX_SOURCE_CHARACTERS_PER_RUN`, default `120000`
- `PERSONA_FACTORY_LLM_MAX_SOURCE_TOKENS_PER_RUN`, default `30000`
- `PERSONA_FACTORY_LLM_MAX_CALLS_PER_RUN`, default `100`
- `PERSONA_FACTORY_LLM_TIMEOUT_SECONDS`, default `15`
- `PERSONA_FACTORY_LLM_RETRY_ATTEMPTS`, default `0`

LLM-backed distillation requires a resolvable structured-output model. With `llm_model_source="main_agent"`, configure an
enabled main-agent profile with a default model profile. With `llm_model_source="model_profile"`, pass
`model_profile_id`. Existing clients that send `distillation_mode="llm"` and only `model_profile_id` continue to map to
`model_profile` source. With `llm_model_source="model"`, pass both `llm_model_provider` and `llm_model`.

When LLM-backed distillation fails:

- `llm` mode marks the run failed and emits a Persona Factory failure projection event.
- `hybrid` mode records warnings and keeps deterministic candidates when possible.
- Retry attempts are opt-in through `PERSONA_FACTORY_LLM_RETRY_ATTEMPTS`; every attempted model call counts toward
  `PERSONA_FACTORY_LLM_MAX_CALLS_PER_RUN`.

Run the local Persona Factory smoke check:

```bash
API=http://localhost:8000 bash scripts/persona_factory_smoke.sh
```

Use `CLEANUP=1` when the smoke persona should be archived and generated memory records removed after the check.

## Persona Graph Projection

Neo4j is a rebuildable projection for inspection and runtime graph context. Persona source of truth remains Postgres and
memory records.

If Persona graph context is empty after publishing:

1. Confirm `GRAPH_PROJECTION_ENABLED=true`.
2. Check pending projection events in the backend logs or graph projector worker.
3. Re-run the graph read endpoint after projection catches up:

```bash
curl -sS "$API/persona/$PERSONA_ID/graph-context?preset=persona_lineage"
```

Graph context should not block Persona distillation, review, publishing, or runtime invocation. Approved package content
and approved persona-scoped memory outrank graph context when they conflict.

## Verification

Focused backend checks for Persona Factory work:

```bash
python -m unittest tests.test_persona_domain_repositories
python -m unittest tests.test_persona_factory_api
python -m unittest tests.test_persona_llm_distillation_schema
python -m unittest tests.test_eval_runner
python -m unittest tests.test_documentation_consistency
```

Run CI-safe extraction and runtime eval fixtures:

```bash
python scripts/run_evals.py --no-write --json
```

For manual LLM-backed Persona validation, run focused live cases against a local backend before broad live benchmarking:

```bash
python scripts/run_evals.py --suite persona_distillation --case-id persona_messy_meeting_notes --live-persona-distillation --base-url http://localhost:8000 --live-llm-model-source main_agent --timeout-seconds 900 --no-write --json
python scripts/run_evals.py --suite persona_distillation --case-id persona_sop_policy --live-persona-distillation --base-url http://localhost:8000 --live-llm-model-source main_agent --timeout-seconds 900 --no-write --json
python scripts/run_evals.py --suite persona_distillation --case-id persona_tool_process --live-persona-distillation --base-url http://localhost:8000 --live-llm-model-source main_agent --timeout-seconds 900 --no-write --json
```
