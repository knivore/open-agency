# Outbound Runtime Webhooks

Agency supports outbound webhook notifications as a runtime side effect. Runtime code can notify external systems about
Agency events without giving those systems authority to mutate workflow state.

Current scope:

- supported: outbound JSON `POST` notifications from Agency to registered external targets
- supported: retries, timeouts, idempotency headers, HMAC signing, bearer auth, audit events, and attempt persistence
- deferred: public inbound third-party webhook endpoints that trigger or resume workflow executions
- separate existing feature: conversation adapter webhooks under `/integrations/conversations/adapters/{provider}/webhook`

## Runtime Boundary

Outbound webhooks are not tool calls and are not public runtime triggers. They are service/runtime utilities used after an
Agency event has already been recorded.

Rules:

- Record workflow or callback state first.
- Send the outbound webhook as best-effort notification.
- Do not let a failed webhook roll back event persistence or checkpoint updates.
- Do not store raw webhook URLs, bearer tokens, HMAC secrets, or secret values in execution events, attempt rows, or logs.
- Do not hardcode destination URLs in workflow definitions.

## Implementation Map

Primary code:

- `app/tools/webhook_client/client.py`
- `app/tools/webhook_client/schemas.py`
- `app/tools/webhook_client/registry.py`
- `app/tools/webhook_client/signer.py`
- `app/domain/webhooks.py`
- `app/db/models/webhooks.py`
- `app/db/repositories/webhooks.py`
- `alembic/versions/20260519_0001_baseline.py`

Tests:

- `tests/test_outbound_webhook_client.py`
- `tests/test_postgres_schema.py`

Related callback integration:

- [Internal Sub-Agent Callbacks](./subagent-callbacks.md)

## Developer Workflow

When adding an outbound runtime notification:

1. Decide which already-persisted Agency event should notify the external system.
2. Register a `WebhookTarget` with environment-backed URL and secret configuration.
3. Inject `OutboundWebhookClient` into the runtime service that owns the side effect.
4. Call `send` only after workflow state, callback state, or execution events have been persisted.
5. Treat `WebhookSendResult.ok=false` as delivery telemetry, not workflow failure.

Do not use this client to let external webhook traffic drive workflow state. Public inbound runtime triggers need a
separate design with authentication, replay protection, rate limiting, and signature verification.

## Target Registration

Webhook targets are represented by `WebhookTarget` and resolved by `WebhookTargetRegistry`.

```python
from app.tools.webhook_client import WebhookAuthType, WebhookTarget, WebhookTargetRegistry

registry = WebhookTargetRegistry(
    [
        WebhookTarget(
            target="discord_ops",
            url_env="DISCORD_OPS_WEBHOOK_URL",
            auth_type=WebhookAuthType.HMAC,
            secret_env="DISCORD_OPS_SIGNING_SECRET",
            default_headers={"Content-Type": "application/json"},
            timeout_seconds=10,
            max_retries=3,
            backoff_seconds=0.25,
        )
    ]
)
```

`url_env`, `token_env`, and `secret_env` are environment variable names. The registry resolves their values at send time.

Supported auth modes:

- `none`: no auth header or signature is added
- `bearer`: sends `Authorization: Bearer <token>` resolved from `token_env`
- `hmac`: sends `X-Agency-Webhook-Timestamp` and `X-Agency-Webhook-Signature` generated from `secret_env`

## Sending

Use `OutboundWebhookClient.send`.

```python
result = await webhook_client.send(
    target="discord_ops",
    event_type="workflow.failed",
    payload={
        "run_id": run_id,
        "workflow_id": workflow_id,
        "message": "Workflow failed at deploy step",
    },
    idempotency_key=f"{run_id}:workflow.failed",
    run_id=run_id,
    workflow_id=workflow_id,
)
```

The outbound request body is canonical JSON:

```json
{
  "event_type": "workflow.failed",
  "payload": {
    "run_id": "execution-1"
  }
}
```

When `idempotency_key` is supplied, it is sent as the `Idempotency-Key` header and stored in audit data. Receiver-side
deduplication is still the receiver's responsibility.

## Result Contract

`WebhookSendResult` returns:

- `ok`
- `target`
- `event_type`
- `status`
- `attempts`
- `idempotency_key`
- `request_payload_sha256`
- `response_status`
- `response_body_preview`
- `error_message`
- `audit_event_ids`
- `created_at`
- `completed_at`

Final delivery failures return `ok=false`; they do not crash workflow execution once target configuration has resolved.
Configuration errors such as missing target URLs or missing secret environment variables fail before a request is sent.

## Retry Policy

The client attempts delivery up to `max_retries + 1` times.

Retryable HTTP status codes:

- `408`
- `425`
- `429`
- `500`
- `502`
- `503`
- `504`

Network and transport exceptions are also retried. Backoff is linear by attempt:

```text
sleep_seconds = backoff_seconds * attempt_no
```

## Audit Events

When `execution_store` is provided and a run ID is available, the client writes execution events:

- `outbound_webhook.queued`
- `outbound_webhook.sent`
- `outbound_webhook.failed`

Audit payloads include:

- target name
- event type
- idempotency key
- request payload SHA-256
- URL hash
- attempt number
- response status
- sanitized response preview
- sanitized error message

Audit events intentionally do not include raw target URLs or secrets.

## Persisted Attempts

When `attempt_repository` is provided, the client writes one `outbound_webhook_attempts` row per delivery attempt.

Stored fields:

- queued audit event ID when available
- target name
- URL hash
- idempotency key
- request payload SHA-256
- response status
- sanitized response body preview
- attempt number
- status
- sanitized error message
- created timestamp

The table stores `url_hash`, not the raw URL.

## Secret Handling

The client sanitizes these values before returning, auditing, or persisting text:

- raw target URL
- URL host
- URL path fragments
- bearer token
- HMAC secret

Payloads are not redacted by the webhook client before they are sent to the external target. Callers must not put secrets
inside payloads.

## Operational Checks

Useful verification commands:

```bash
PYTHONPATH=. .venv/bin/python -m unittest tests.test_outbound_webhook_client
PYTHONPATH=. .venv/bin/python -m unittest tests.test_postgres_schema
```

For a live smoke test, create a local HTTP receiver, register a target whose `url_env` points at that receiver, and call
`OutboundWebhookClient.send`. Confirm that:

- the receiver gets a JSON `POST`
- `Idempotency-Key` is present when configured
- an attempt row is recorded
- response preview and errors are sanitized

## Not Implemented Here

The outbound webhook client does not provide:

- public inbound webhook endpoints
- workflow-triggering webhooks
- user-facing frontend target configuration
- receiver-side idempotency enforcement
- generic agent-callable HTTP execution

Agent-callable HTTP requests continue to use the existing HTTP tool contract. Runtime outbound webhooks use this client
because they need runtime audit, retry, and secret-handling behavior.
