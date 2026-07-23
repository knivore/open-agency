# Internal Sub-Agent Callbacks

Sub-agent callbacks are internal runtime events that let a sub-agent report progress, completion, failure, input needs, or
approval needs back to the supervisor/main-agent runtime.

Current scope:

- supported: internal service calls through `SubAgentCallbackService`
- supported: execution event persistence, checkpoint updates, dispatcher pause/resume hooks, idempotency, retry metadata,
  and optional outbound webhook notifications
- supported: durable input and approval wait records linked to callback checkpoints
- deferred: public third-party runtime webhook ingestion
- forbidden: sub-agents directly calling supervisor/main-agent functions to mutate state

## Runtime Boundary

Callbacks are event-driven runtime updates. The callback service is the write boundary:

```text
Sub-agent
  -> SubAgentCallbackService
  -> Runtime event envelope
  -> Execution event store
  -> Durable execution wait for input/approval callbacks
  -> Execution metadata checkpoint
  -> Dispatcher pause/resume when appropriate
  -> Optional outbound webhook notification
```

Rules:

- Every callback must write an event before updating checkpoint metadata.
- Checkpoint changes must go through the execution store.
- Dispatcher calls are follow-up actions, not the source of truth.
- Optional outbound notifications are best-effort and must not mutate workflow state.
- The service is internal code, not a public external webhook endpoint.

## Implementation Map

Primary code:

- `app/runtime/callbacks/callback_service.py`
- `app/runtime/callbacks/callback_schemas.py`
- `app/runtime/events/factory.py`
- `app/runtime/events/payloads.py`
- `app/domain/events.py`
- `app/runtime/native/state.py`

Tests:

- `tests/test_subagent_callback_service.py`
- `tests/test_runtime_event_model.py`

Related outbound notification docs:

- [Outbound Runtime Webhooks](./outbound-webhooks.md)

## Developer Workflow

When wiring a sub-agent callback path:

1. Inject the execution store so the service can write an execution event and checkpoint metadata.
2. Inject the dispatcher when callbacks should pause or resume runtime work.
3. Inject the workflow repository when completed sub-agent steps should calculate ready dependent steps.
4. Inject the outbound webhook client only when pause states need external notification.
5. Pass an `idempotency_key` for callback sources that may retry delivery.

The callback service is the state mutation boundary. Sub-agents should call this service rather than directly invoking
supervisor or main-agent methods.

## Callback Methods

`SubAgentCallbackService` exposes:

- `record_subagent_progress`
- `record_subagent_status`
- `record_subagent_completed`
- `record_subagent_failed`
- `record_subagent_needs_input`
- `record_subagent_needs_approval`

All methods accept:

- `run_id`
- `step_id`
- `agent_id`
- optional `workflow_id`
- optional `payload`
- optional `source`
- optional `idempotency_key`

Example:

```python
receipt = await callback_service.record_subagent_completed(
    run_id="execution-1",
    workflow_id="workflow-1",
    agent_id="agent-1",
    step_id="task-1",
    payload={"result": "done"},
    idempotency_key="execution-1:task-1:completed",
)
```

Receipt:

```json
{
  "ok": true,
  "event_id": "uuid",
  "run_id": "execution-1",
  "step_id": "task-1",
  "status": "recorded",
  "created_at": "datetime"
}
```

## Structured Status Updates

`record_subagent_progress` remains backward compatible with arbitrary payloads. When the payload contains structured
status fields, the service validates them with `SubAgentStatusUpdate`, writes normalized fields at the top level of the
execution event payload, and stores the latest structured values on the checkpoint.

Structured fields:

- `status`
- `current_task`
- `completed_step`
- `blocker`
- `clarification_needed`
- `confidence`
- `token_usage`
- `context_health`
- `tool_result_summary`
- `next_action`
- `progress_percent`

The status update can be sent either as top-level payload fields:

```json
{
  "status": "blocked",
  "current_task": "Validate rollout plan",
  "blocker": "Missing production window",
  "confidence": 0.4,
  "progress_percent": 50,
  "token_usage": {
    "prompt_tokens": 100,
    "completion_tokens": 40,
    "total_tokens": 140
  },
  "context_health": {
    "estimated_prompt_tokens": 1200,
    "reserved_completion_tokens": 512,
    "estimated_total_context_tokens": 1712,
    "context_window": 8192,
    "status": "normal"
  }
}
```

or nested under `status_update`:

```json
{
  "message": "Halfway through validation.",
  "status_update": {
    "status": "working",
    "current_task": "Compare candidate outputs",
    "progress_percent": 45,
    "next_action": "Summarize findings"
  }
}
```

`record_subagent_status` is a convenience wrapper around `record_subagent_progress` for callers that already have typed
status fields rather than an arbitrary payload object.

## Event Model

Callback methods write these event types:

- `subagent.progress.updated`
- `subagent.step.completed`
- `subagent.step.failed`
- `subagent.needs_input`
- `subagent.needs_approval`

Each runtime event envelope includes:

- event ID
- event type
- run ID
- workflow ID when available
- agent ID
- step ID
- source
- status
- payload
- payload SHA-256
- created timestamp

The current implementation maps runtime event envelopes into the existing execution event stream. `source`, `status`, and
`payload_sha256` are persisted on execution events rather than in a separate `runtime_events` table.

## Checkpoint Metadata

The service writes checkpoint metadata under `execution.metadata.runtime_callbacks`.

```json
{
  "runtime_callbacks": {
    "checkpoints": {
      "task-1": {
        "agent_id": "agent-1",
        "step_id": "task-1",
        "status": "completed",
        "event_id": "uuid",
        "event_type": "subagent.step.completed",
        "source": "subagent:agent-1",
        "payload_sha256": "hex",
        "updated_at": "datetime",
        "status_update": {
          "status": "working",
          "current_task": "Compare candidate outputs",
          "confidence": 0.7,
          "progress_percent": 45
        },
        "subagent_status": "working",
        "current_task": "Compare candidate outputs",
        "confidence": 0.7,
        "progress_percent": 45
      }
    },
    "last_event_id": "uuid",
    "last_event_type": "subagent.step.completed",
    "updated_at": "datetime"
  }
}
```

Pause callbacks also write:

- `execution.metadata.pending_subagent_input`
- `execution.metadata.pending_subagent_approval`
- `execution.metadata.active_wait`

Input and approval callbacks create one pending `execution_waits` row with the callback event and subagent step in its
metadata. They also emit `execution.waiting` after the callback event. The wait row is the durable wake claim; callback
metadata remains the supervisor-facing summary.

## Dispatcher Behavior

Progress:

- event status: `running`
- checkpoint status: `running`
- dispatcher action: none
- structured status fields are normalized into event payload and checkpoint metadata when present

Completed:

- event status: `completed`
- checkpoint status: `completed`
- dependency check: calculates `ready_dependent_step_ids` when a workflow repository is available
- dispatcher action: `resume(run_id)`

Failed:

- event status: `failed`
- checkpoint status: `failed` when no retry is available
- checkpoint status: `retry_queued` when retry policy allows another attempt
- dispatcher action: `resume(run_id)` only for retry-eligible failures

Needs input:

- event status: `queued`
- checkpoint status: `needs_input`
- execution status: `waiting_for_input`
- durable wait kind: `input`
- dispatcher action: `pause(run_id)`
- optional outbound webhook notification

Needs approval:

- event status: `queued`
- checkpoint status: `needs_approval`
- execution status: `waiting_for_approval`
- durable wait kind: `approval`
- dispatcher action: `pause(run_id)`
- optional outbound webhook notification

## Retry Policy

Retry policy can be supplied in callback payload:

```json
{
  "retry_policy": {
    "max_retries": 2
  }
}
```

It can also be stored on execution metadata:

```json
{
  "runtime_callbacks": {
    "retry_policies": {
      "task-1": {"max_retries": 2}
    },
    "retry_policy": {"max_retries": 1}
  }
}
```

Precedence:

1. `payload.retry_policy`
2. `metadata.runtime_callbacks.retry_policies[step_id]`
3. `metadata.runtime_callbacks.retry_policy`

Each failed callback increments retry attempts stored on the checkpoint.

## Idempotency

When `idempotency_key` is supplied, the service stores the receipt under:

```json
{
  "runtime_callbacks": {
    "idempotency": {
      "execution-1:task-1:completed": {
        "ok": true,
        "event_id": "uuid",
        "run_id": "execution-1",
        "step_id": "task-1",
        "status": "recorded",
        "created_at": "datetime"
      }
    }
  }
}
```

A repeated callback with the same idempotency key returns the stored receipt and does not create a duplicate execution
event, checkpoint update, dispatcher action, or outbound notification.

## Optional Outbound Notifications

Input and approval pauses can notify an outbound webhook target when `SubAgentCallbackService` is constructed with a
webhook client.

Payload-level target:

```json
{
  "question": "Which region should I deploy to?",
  "outbound_webhook_target": "discord_ops"
}
```

Nested target:

```json
{
  "outbound_webhook": {
    "target": "approval_ops"
  }
}
```

Execution metadata target:

```json
{
  "runtime_callbacks": {
    "outbound_webhooks": {
      "subagent.needs_input": {"target": "discord_ops"},
      "subagent.needs_approval": {"target": "approval_ops"},
      "default": {"target": "ops_default"}
    }
  }
}
```

Target lookup order:

1. `payload.outbound_webhook_target`
2. `payload.outbound_webhook.target`
3. `metadata.runtime_callbacks.outbound_webhooks[event_type]`
4. `metadata.runtime_callbacks.outbound_webhooks[status]`
5. `metadata.runtime_callbacks.outbound_webhooks.default`

Webhook payloads include:

- run ID
- workflow ID
- agent ID
- step ID
- checkpoint status
- original callback payload
- payload SHA-256

Notifications are best-effort. A notification failure does not fail the callback.

## Operational Checks

Useful verification commands:

```bash
PYTHONPATH=. .venv/bin/python -m unittest tests.test_subagent_callback_service
PYTHONPATH=. .venv/bin/python -m unittest tests.test_runtime_event_model
```

For manual inspection, load an execution after a callback and confirm:

- an execution event with the expected `subagent.*` type exists
- `metadata.runtime_callbacks.checkpoints[step_id]` has the expected status
- pause callbacks set the execution status correctly
- pause callbacks create one pending durable wait and emit `execution.waiting`
- duplicate idempotency keys return the original receipt
- optional webhook notifications do not block callback recording

## Not Implemented Here

Internal sub-agent callbacks do not provide:

- public external webhook ingestion
- public callback URLs for third-party systems
- direct supervisor/main-agent function mutation
- durable replay of arbitrary external callback traffic
- frontend UI for callback injection

Public inbound runtime triggers should be designed separately with authentication, replay protection, rate limiting,
signature verification, and clear separation from conversation adapter webhooks.
