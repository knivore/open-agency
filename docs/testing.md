# Testing

## Overview

The backend test suite is split across:

- API startup and route registration tests
- domain model serialization tests
- repository and database foundation tests
- Alembic migration tests
- runtime, scheduler, tool, MCP, and A2A tests
- architecture checks for migrated import boundaries

Tests are designed to avoid real cloud LLM calls. Runtime and provider behavior is mocked or exercised through local
fake implementations.

## Prerequisites

Create and activate the project virtual environment, then install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Environment variables

Most tests only need:

```bash
APP_ENV=test
```

Database-backed tests use an isolated SQLite URL automatically, but the app also supports:

```bash
DATABASE_URL=sqlite+aiosqlite:///:memory:
DATABASE_ECHO=false
DATABASE_POOL_SIZE=5
DATABASE_MAX_OVERFLOW=10
```

You do not need real cloud LLM credentials to run the test suite.

## Run all tests

```bash
make test
```

Or directly:

```bash
./.venv/bin/python -m unittest
```

## Run database and migration tests

Database foundation and repository tests:

```bash
./.venv/bin/python -m unittest tests.test_database_foundation tests.test_postgres_schema tests.test_storage_migration
```

Alembic migration validation is covered by:

```bash
./.venv/bin/python -m unittest tests.test_postgres_schema
```

## Run API and runtime tests

```bash
./.venv/bin/python -m unittest tests.test_api_main tests.test_workflow_builder_api tests.test_execution_control_plane tests.test_native_execution_engine tests.test_scheduler
```

## Run architecture checks

```bash
make check-architecture
```

Or directly:

```bash
./.venv/bin/python -m unittest tests.test_legacy_import_check tests.test_architecture_validation
```

## Run compile and architecture validation

```bash
make lint
```

This currently performs:

- Python bytecode compilation for `app` and `tests`
- architecture guard checks

## Run Evaluation Smoke Suite

The default eval suite is deterministic and does not require a live backend or LLM credentials:

```bash
make eval
```

Or directly:

```bash
./.venv/bin/python scripts/run_evals.py
```

Useful options:

```bash
./.venv/bin/python scripts/run_evals.py --suite smoke --json
```

```bash
make eval EVAL_ARGS="--suite smoke --json --no-write"
```

```bash
./.venv/bin/python scripts/run_evals.py --base-url http://localhost:8000 --judge-agent
```

The runner loads cases from `evals/cases`, evaluates deterministic assertions, appends JSONL results to
`.data/evals/results.jsonl`, and can invoke the read-only Evaluation agent as a semantic judge for backend-backed runs.
Eval cases can run from offline fixtures, an existing execution id, or a live workflow start through `http_workflow`.
The default cases under `evals/cases` should stay offline and CI-safe; put backend-specific one-off cases elsewhere and
pass them with `--cases`.

## Local development server

```bash
make dev
```

## Validate runtime paths against a running backend

Use the runtime validation script when you want to exercise the real canonical HTTP execution routes instead of only the
in-process test suite.

Default command:

```bash
make validate-runtimes
```

This script:

- creates temporary canonical workflow and model-profile payloads
- starts executions through `/workflows/{workflow_id}/executions/start`
- polls `/executions/{execution_id}`
- fetches `/events` and `/artifacts`
- can exercise `/executions/{execution_id}/stream`, `/hitl/stream`, and `/hitl/reply` for CrewAI validation
- exits non-zero if a selected runtime does not complete successfully

Examples:

```bash
./.venv/bin/python scripts/validate_runtime_paths.py --runtime native --provider ollama --model llama3:8b
```

```bash
./.venv/bin/python scripts/validate_runtime_paths.py --runtime crewai --provider openai_compatible --model my-model --model-base-url http://localhost:1234/v1 --api-key local-key
```

```bash
./.venv/bin/python scripts/validate_runtime_paths.py --runtime crewai --provider ollama --model gpt-oss:20b --model-base-url http://host.docker.internal:11434 --exercise-hitl
```

## Notes

- The legacy root tool folder has been removed. Architecture checks now enforce that app-owned modules do not
  reintroduce imports from deleted roots such as `models`, `routers`, `tools_directory`, or `database`.
- The CrewAI runtime path now uses model-profile configuration rather than an Azure-only env assumption, but successful
  live `crewai` validation still requires a reachable provider endpoint and valid credentials for the selected profile.
- When the backend runs on the host with `./run.sh start`, use `localhost` for host Ollama. When the backend
  runs inside Docker and Ollama runs on the host machine, prefer `host.docker.internal` over `localhost`.
- When Redis is unavailable in local or test runs, the HITL channel layer now falls back to an in-process pubsub
  implementation so the validation script can still exercise the canonical HITL APIs.
