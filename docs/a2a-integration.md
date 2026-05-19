# A2A Integration

## Overview

The backend now includes an additive Agent2Agent layer under `app/protocols/a2a`. A2A is mapped onto the canonical
backend model instead of introducing a separate execution system.

## Canonical Mapping

- `AgentDefinition` -> A2A Agent Card
- `Execution` -> A2A Task
- `ExecutionEvent` -> A2A Message
- `ExecutionArtifact` -> A2A Artifact

This keeps A2A as a protocol adapter over the existing execution and event store.

## Components

- `agent_card.py`: converts `AgentDefinition` into an A2A-compatible agent card.
- `tasks.py`: maps canonical `Execution` objects to A2A tasks.
- `messages.py`: maps `ExecutionEvent` records to A2A messages.
- `artifacts.py`: maps `ExecutionArtifact` records to A2A artifacts.
- `adapter.py`: shared helpers for task/message/artifact mapping and remote-agent calls.
- `routes.py`: exposes A2A routes.

## Routes

- `GET /.well-known/agent-card.json`
- `POST /a2a/tasks`
- `GET /a2a/tasks/{id}`
- `POST /a2a/tasks/{id}/messages`
- `GET /a2a/tasks/{id}/artifacts`

## Task Lifecycle

`POST /a2a/tasks` creates a canonical `Execution`. If a message is included, it is recorded as an
`agent.message.created` event against that execution.

`POST /a2a/tasks/{id}/messages` appends an `ExecutionEvent` and can optionally attach an `ExecutionArtifact`.

`GET /a2a/tasks/{id}` and `GET /a2a/tasks/{id}/artifacts` read from the existing execution and artifact store.

## Remote A2A Agent Tool

`ToolDefinition.tool_type = "a2a_remote_agent"` is now supported.

Expected behavior:

- input schema maps to an outbound A2A message/task request
- output maps to the remote task response or produced artifact payload

Security expectations:

- remote hosts must be allowlisted via `security.allowlisted_domains`
- network access must remain explicit
- test and development flows can use `implementation.config.stub_response`

This keeps remote-agent calls visible to the normal tool execution and audit event pipeline.
