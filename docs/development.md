# Development

## Overview

New backend work should be added under `app/`. Avoid introducing new root-level architecture folders or new imports from
archived legacy paths.

Recommended validation commands:

- `make test`
- `make lint`
- `make check-architecture`
- `./.venv/bin/python -m unittest tests.test_legacy_import_check tests.test_architecture_validation`

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
3. Register the tool in `app/tools/definitions.py` or the database-backed tool catalog.
4. Add implementation code under `app/tools/implementations/`.
5. Add or update an executor under `app/tools/executors/` if the tool type is new.
6. Declare security metadata, approval requirements, validation rules, and parameter descriptions detailed enough for
   agents to select and call the tool without guessing.
7. Add tests for importability, validation, and execution behavior.

## Adding A New Workflow

1. Extend the canonical workflow model in `app/domain/workflows.py` if needed.
2. Persist workflow and version changes through `app/db/models/workflows.py` and repositories.
3. Expose API changes through `app/api/routes/workflows.py`.
4. Ensure runtimes can consume the resulting workflow definition.
5. Add tests for CRUD, versioning, and runtime execution.

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
