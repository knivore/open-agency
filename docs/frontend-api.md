# Frontend API

## Overview

The frontend should treat the `app/api` routes as the canonical backend API. Workflow execution should use
workflow/execution routes directly; do not build new UI flows around retired Crew, history, artifact, or HITL
compatibility namespaces.

Current contract status:

- active frontend/backend integration is canonical on `workflow`, `instructions`, and `workflowId`
- runtime-specific mapping, including CrewAI field translation, happens at the backend adapter boundary
- retired compatibility namespaces are not part of the maintained frontend API guidance
- workflow ownership and auth stay the same whether execution uses `native` or `crewai`

## Key Endpoints

Catalog endpoints:

- `GET /agents`
- `POST /agents`
- `GET /tools`
- `POST /tools`
- `GET /tools/contracts`
- `GET /tools/contracts/{tool_name}`
- `POST /tools/{tool_name}/run`
- `GET /model-providers`
- `POST /model-providers`
- `GET /model-profiles`
- `POST /model-profiles`
- `POST /documents/ingest`
- Document ingestion accepts multipart uploads and creates `archive` memory chunks with document provenance metadata.
- Memory records may include embedding metadata: `embedding_model_profile_id`, `embedding_model`,
  `embedding_dimensions`, and `embedded_at`.
- `POST /model-providers/{provider_id}/authorize`
- `POST /model-providers/{provider_id}/callback-complete`
- `POST /model-providers/{provider_id}/device-authorize`
- `POST /model-providers/{provider_id}/device-complete`
- `GET /runtime-adapters`
- `GET /mcp-servers`
- `GET /a2a/agents`

Tool records expose a split identity contract. `id` is the stable persisted identity, `name` is the callable-safe
agent/runtime name, and `display_name` is the frontend label. UI code should render `display_name` through the frontend
tool display helper and should not show raw implementation/callable metadata in normal user-facing views.

Workflow and execution endpoints:

- `GET /workflows`
- `POST /workflows`
- `GET /workflows/{workflow_id}`
- `GET /workflows/{workflow_id}/shared-memory`
- `PATCH /workflows/{workflow_id}/shared-memory`
- `GET /workflows/{workflow_id}/versions`
- `GET /workflows/{workflow_id}/versions/{revision}`
- `POST /executions`
- `POST /workflows/{workflow_id}/executions/start`
- `GET /executions`
- `GET /executions/{execution_id}`
- `GET /executions/{execution_id}/events`
- `GET /executions/{execution_id}/stream`
- `GET /executions/{execution_id}/hitl/stream`
- `POST /executions/{execution_id}/hitl/reply`
- `GET /executions/{execution_id}/artifacts`
- `POST /executions/{execution_id}/cancel`
- `POST /schedules`
- `GET /schedules`

Observability and health:

- `GET /health`
- `GET /health/db`
- `GET /observability/executions/{execution_id}`

## Workflow Builder Flow

Typical frontend flow for workflow authoring:

1. Load tool, agent, model profile, and runtime adapter catalogs.
2. Create or update a workflow definition.
3. Keep `native` as the default runtime unless there is a clear reason to allow another adapter.
4. Persist a versioned workflow through the workflows API.
5. Optionally allow `crewai` or another adapter for workflows that should support alternate runtimes.
6. Publish or activate the desired version.
7. Trigger a test execution against the selected runtime adapter.

The workflow domain definition is the contract between the builder UI, persistence, and runtimes.

Shared-memory controls:

- Use `GET /workflows/{workflow_id}/shared-memory` to load the workflow shared-memory operator payload.
- Use `PATCH /workflows/{workflow_id}/shared-memory` with `enabled`, optional `limit_per_layer`, and optional
  `apply_to_agents=true` to update shared-memory settings without submitting a full workflow definition.

Workflow version history:

- `GET /workflows/{workflow_id}/versions` returns reverse-revision ordered items with `revision`, semantic `version`,
  `status`, `is_current`, `is_published`, timestamps, provenance, and the persisted `definition`.
- `GET /workflows/{workflow_id}/versions/{revision}` returns one persisted revision or `404` when the workflow or revision
  does not exist.

## Execution Control Flow

Typical execution lifecycle:

1. Frontend submits `POST /executions` with workflow reference, runtime adapter, and input payload.
2. Backend creates an execution record and initial execution events.
3. Runtime adapter runs the workflow and appends events, artifacts, approvals, and tool invocations.
4. Frontend polls execution detail or event endpoints to render progress.
5. Final output and status are read from the execution record and event stream.

## Event Streaming Flow

The internal source of truth is the execution event log.

Frontend-facing behavior is built from:

- `GET /executions/{execution_id}/events`
- observability endpoints that aggregate the event stream
- `GET /executions/{execution_id}/stream` for live updates

If a screen needs timeline reconstruction, event-level details should be preferred over derived summary fields.

## Approval Flow

Approval-aware tools expose security metadata in their definitions. The expected UI flow is:

1. Frontend starts an execution that includes approval-gated tools.
2. Runtime creates an approval request when a gated tool is about to execute.
3. Frontend reads approval state from execution and event data.
4. User approves or rejects.
5. Runtime resumes and records the decision in execution events and approval request state.

For interactive CrewAI runs, the canonical HITL endpoints are:

- `GET /executions/{execution_id}/hitl/stream`
- `POST /executions/{execution_id}/hitl/reply`

## LLM OAuth Flow

For OAuth-capable model providers, the FE should use the model-provider routes as the canonical auth API.

Provider-side state:

- OAuth tokens live in `model_provider.config`
- multi-account OAuth state lives in `model_provider.config.auth_profiles`
- the default account lives in `model_provider.config.default_oauth_profile_id`
- a model profile can optionally point at a specific account using `model_profile.parameters.oauth_profile_id`

Frontend behavior for the `LLM Models` page:

1. Load provider and profile catalogs.
2. Render OAuth status from the provider record, not from copied profile token fields.
3. When the user starts ChatGPT OAuth, call `POST /model-providers/{provider_id}/authorize`.
4. Preserve the returned `pkce_verifier`, `state`, `redirect_uri`, and `auth_profile_id` in UI state until
   completion.
5. Complete the flow with `POST /model-providers/{provider_id}/callback-complete`.
6. If loopback capture fails, allow the user to paste the full redirect URL and send it as `redirect_url`.
7. For headless flow, use `device-authorize` and `device-complete`.
8. Refresh the provider record after completion and re-render connection-level OAuth status.

Expected request shapes:

```json
POST /model-providers/{provider_id}/authorize
{
  "auth_profile_id": "default"
}
```

```json
POST /model-providers/{provider_id}/callback-complete
{
  "code": "...",
  "pkce_verifier": "...",
  "state": "...",
  "auth_profile_id": "default"
}
```

```json
POST /model-providers/{provider_id}/callback-complete
{
  "redirect_url": "http://127.0.0.1:1455/auth/callback?code=...&state=...",
  "auth_profile_id": "default"
}
```

```json
POST /model-providers/{provider_id}/device-authorize
{
  "auth_profile_id": "default"
}
```

```json
POST /model-providers/{provider_id}/device-complete
{
  "device_code": "...",
  "auth_profile_id": "default"
}
```

UI guidance:

- show one provider connection with zero or more OAuth profiles/accounts beneath it
- show `account_id` when returned by the backend
- allow setting which OAuth profile is the provider default
- let a model preset opt into a specific OAuth profile only when needed
- avoid editing raw token fields in the FE

## Conversation Model Auth Recovery

Main-agent chat is LLM-first for plain user text. The frontend should submit normal chat messages to
`POST /conversations/{conversation_id}/messages` and let the backend resolve the active main-agent model profile. The
configured model can be Codex, Ollama, or any registered provider.

Browser chat should send `response_mode: "async"`. The backend returns `200 OK` after persisting the user message and
includes a `stream_url`; the assistant response then arrives through `GET /conversations/{conversation_id}/stream`.
Avoid long synchronous browser/proxy waits for LLM calls because model planning and tool use can exceed frontend reverse
proxy timeouts. Treat the stream as the live path, but also backfill from `GET /conversations/{conversation_id}/messages`
after async sends until a message appears after the submitted user message. This keeps the UI correct when a local/dev
SSE connection misses an in-memory event during backend reloads or process changes.

When the active model cannot be used because provider auth is missing, expired, or lacks required scopes, the backend
should still return `200 OK` with an `assistant_text` message. The frontend should inspect:

```json
{
  "assistant_message": {
    "metadata": {
      "model_auth": {
        "auth_required": true,
        "reauthorization_required": true,
        "auth_action": "device_authorize",
        "auth_endpoint": "/model-providers/openai-codex/device-authorize",
        "provider_id": "openai-codex"
      }
    }
  }
}
```

If `reauthorization_required` is true, render a model re-auth action and call `auth_endpoint` using the provider's normal
OAuth/device-flow request shape. Do not special-case all assistant failures as Codex failures; the backend supplies
`provider_id`, `auth_action`, and `auth_endpoint` for the active model.

## Runtime Namespace Boundary

Frontend code should use the canonical workflow, execution, tool, model, credential, conversation, document, voice,
MCP, and A2A route groups documented above. Do not build new frontend features against old compatibility namespaces such
as:

- `/api/crew/*`
- `/api/history/*`
- `/api/artifacts/*`
- `/api/hitl/*`
