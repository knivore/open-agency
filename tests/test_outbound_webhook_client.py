from __future__ import annotations

import hmac
import unittest
from hashlib import sha256

import httpx

from app.db.repositories.webhooks import InMemoryOutboundWebhookAttemptRepository
from app.domain import Execution, ExecutionEventType, ExecutionStatus
from app.runtime.native.state import InMemoryExecutionStore
from app.tools.webhook_client.client import OutboundWebhookClient
from app.tools.webhook_client.registry import WebhookTargetRegistry
from app.tools.webhook_client.schemas import WebhookAuthType, WebhookTarget
from app.tools.webhook_client.signer import build_hmac_headers, sign_body


class FakeWebhookTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    async def post(self, url: str, *, content: bytes, headers: dict[str, str], timeout: float) -> httpx.Response:
        self.requests.append({"url": url, "content": content, "headers": headers, "timeout": timeout})
        next_response = self.responses.pop(0)
        if isinstance(next_response, Exception):
            raise next_response
        return next_response


class OutboundWebhookClientTests(unittest.IsolatedAsyncioTestCase):
    def _registry(self, *, auth_type: WebhookAuthType = WebhookAuthType.NONE) -> WebhookTargetRegistry:
        target = WebhookTarget(
            target="discord_ops",
            url_env="DISCORD_OPS_WEBHOOK_URL",
            auth_type=auth_type,
            token_env="DISCORD_OPS_TOKEN" if auth_type == WebhookAuthType.BEARER else None,
            secret_env="DISCORD_OPS_SIGNING_SECRET" if auth_type == WebhookAuthType.HMAC else None,
            default_headers={"Content-Type": "application/json", "X-Default": "yes"},
            timeout_seconds=3,
            max_retries=2,
            backoff_seconds=0,
        )
        return WebhookTargetRegistry(
            [target],
            environ={
                "DISCORD_OPS_WEBHOOK_URL": "https://hooks.example.test/discord/secret-url",
                "DISCORD_OPS_TOKEN": "super-secret-token",
                "DISCORD_OPS_SIGNING_SECRET": "super-secret-signing-key",
            },
        )

    async def _store(self) -> InMemoryExecutionStore:
        store = InMemoryExecutionStore()
        await store.save_execution(
            Execution(
                id="execution-1",
                workflow_id="workflow-1",
                runtime_adapter_id="native",
                status=ExecutionStatus.RUNNING,
                input_payload={},
            )
        )
        return store

    async def test_successful_send_posts_json_and_records_audit_events(self) -> None:
        store = await self._store()
        attempt_repo = InMemoryOutboundWebhookAttemptRepository()
        transport = FakeWebhookTransport([httpx.Response(204, text="")])
        client = OutboundWebhookClient(
            registry=self._registry(),
            execution_store=store,
            attempt_repository=attempt_repo,
            transport=transport,
        )

        result = await client.send(
            target="discord_ops",
            event_type="workflow.failed",
            payload={"run_id": "execution-1", "workflow_id": "workflow-1", "message": "failed"},
            idempotency_key="execution-1:workflow.failed",
        )

        events = await store.list_events("execution-1")
        attempts = await attempt_repo.list_attempts(target="discord_ops")

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "sent")
        self.assertEqual(result.response_status, 204)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(transport.requests[0]["headers"]["Idempotency-Key"], "execution-1:workflow.failed")
        self.assertEqual(transport.requests[0]["headers"]["X-Default"], "yes")
        self.assertEqual([event.event_type for event in events], [
            ExecutionEventType.OUTBOUND_WEBHOOK_QUEUED,
            ExecutionEventType.OUTBOUND_WEBHOOK_SENT,
        ])
        self.assertEqual(events[0].payload["target"], "discord_ops")
        self.assertIn("url_hash", events[0].payload)
        self.assertNotIn("secret-url", str(events[0].payload))
        self.assertEqual(result.audit_event_ids, [event.id for event in events])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].event_id, events[0].id)
        self.assertEqual(attempts[0].status, "sent")
        self.assertEqual(attempts[0].attempt_no, 1)
        self.assertEqual(attempts[0].response_status, 204)
        self.assertEqual(attempts[0].idempotency_key, "execution-1:workflow.failed")
        self.assertNotIn("secret-url", attempts[0].url_hash)

    async def test_retries_transient_response_then_succeeds(self) -> None:
        attempt_repo = InMemoryOutboundWebhookAttemptRepository()
        transport = FakeWebhookTransport([httpx.Response(503, text="try later"), httpx.Response(200, text="ok")])
        client = OutboundWebhookClient(registry=self._registry(), attempt_repository=attempt_repo, transport=transport)

        result = await client.send(
            target="discord_ops",
            event_type="workflow.completed",
            payload={"run_id": "execution-1"},
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.response_status, 200)
        self.assertEqual(len(transport.requests), 2)
        attempts = await attempt_repo.list_attempts(target="discord_ops")
        self.assertEqual([attempt.status for attempt in attempts], ["failed", "sent"])
        self.assertEqual([attempt.response_status for attempt in attempts], [503, 200])

    async def test_failure_returns_failed_result_without_raising(self) -> None:
        store = await self._store()
        attempt_repo = InMemoryOutboundWebhookAttemptRepository()
        transport = FakeWebhookTransport([httpx.Response(503, text="try later"), httpx.Response(500, text="bad")])
        registry = self._registry()
        registry.get("discord_ops").max_retries = 1
        client = OutboundWebhookClient(
            registry=registry,
            execution_store=store,
            attempt_repository=attempt_repo,
            transport=transport,
        )

        result = await client.send(
            target="discord_ops",
            event_type="workflow.failed",
            payload={"run_id": "execution-1"},
        )

        events = await store.list_events("execution-1")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.response_status, 500)
        self.assertEqual(events[-1].event_type, ExecutionEventType.OUTBOUND_WEBHOOK_FAILED)
        attempts = await attempt_repo.list_attempts(target="discord_ops")
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(attempt.status == "failed" for attempt in attempts))

    async def test_bearer_token_is_sent_but_not_leaked_to_result(self) -> None:
        attempt_repo = InMemoryOutboundWebhookAttemptRepository()
        transport = FakeWebhookTransport(
            [
                RuntimeError("failed with super-secret-token at secret-url"),
                RuntimeError("failed with super-secret-token at secret-url"),
                RuntimeError("failed with super-secret-token at secret-url"),
            ]
        )
        client = OutboundWebhookClient(
            registry=self._registry(auth_type=WebhookAuthType.BEARER),
            attempt_repository=attempt_repo,
            transport=transport,
        )

        result = await client.send(
            target="discord_ops",
            event_type="workflow.failed",
            payload={"run_id": "execution-1"},
        )

        self.assertEqual(transport.requests[0]["headers"]["Authorization"], "Bearer super-secret-token")
        self.assertFalse(result.ok)
        self.assertNotIn("super-secret-token", result.error_message or "")
        self.assertNotIn("secret-url", result.error_message or "")
        self.assertIn("[REDACTED_TOKEN]", result.error_message or "")
        attempts = await attempt_repo.list_attempts(target="discord_ops")
        self.assertEqual(len(attempts), 3)
        self.assertNotIn("super-secret-token", attempts[-1].error_message or "")
        self.assertNotIn("secret-url", attempts[-1].error_message or "")

    async def test_response_previews_are_sanitized_before_audit_and_persistence(self) -> None:
        store = await self._store()
        attempt_repo = InMemoryOutboundWebhookAttemptRepository()
        transport = FakeWebhookTransport([httpx.Response(200, text="ok super-secret-token secret-url")])
        client = OutboundWebhookClient(
            registry=self._registry(auth_type=WebhookAuthType.BEARER),
            execution_store=store,
            attempt_repository=attempt_repo,
            transport=transport,
        )

        result = await client.send(
            target="discord_ops",
            event_type="workflow.completed",
            payload={"run_id": "execution-1", "workflow_id": "workflow-1"},
        )

        events = await store.list_events("execution-1")
        attempts = await attempt_repo.list_attempts(target="discord_ops")
        preview_values = [
            result.response_body_preview or "",
            attempts[0].response_body_preview or "",
            events[-1].payload["response_body_preview"],
        ]

        self.assertTrue(result.ok)
        for preview in preview_values:
            self.assertNotIn("super-secret-token", preview)
            self.assertNotIn("secret-url", preview)
            self.assertIn("[REDACTED_TOKEN]", preview)
            self.assertIn("[REDACTED_URL]", preview)

    async def test_hmac_headers_are_added(self) -> None:
        transport = FakeWebhookTransport([httpx.Response(200, text="ok")])
        client = OutboundWebhookClient(
            registry=self._registry(auth_type=WebhookAuthType.HMAC),
            transport=transport,
        )

        result = await client.send(
            target="discord_ops",
            event_type="workflow.failed",
            payload={"run_id": "execution-1"},
        )

        headers = transport.requests[0]["headers"]
        timestamp = headers["X-Agency-Webhook-Timestamp"]
        expected = sign_body(
            "super-secret-signing-key",
            transport.requests[0]["content"],
            timestamp=timestamp,
        )
        self.assertTrue(result.ok)
        self.assertEqual(headers["X-Agency-Webhook-Signature"], expected)

    def test_hmac_signature_generation_is_deterministic(self) -> None:
        body = b'{"event_type":"workflow.failed"}'
        timestamp = "2026-05-24T00:00:00+00:00"

        signature = sign_body("secret", body, timestamp=timestamp)
        headers = build_hmac_headers("secret", body, timestamp=timestamp)
        expected = "sha256=" + hmac.new(b"secret", timestamp.encode("utf-8") + b"." + body, sha256).hexdigest()

        self.assertEqual(signature, expected)
        self.assertEqual(headers["X-Agency-Webhook-Signature"], expected)
        self.assertEqual(headers["X-Agency-Webhook-Timestamp"], timestamp)

    async def test_missing_target_env_fails_before_http_request(self) -> None:
        registry = WebhookTargetRegistry(
            [WebhookTarget(target="missing", url_env="MISSING_WEBHOOK_URL")],
            environ={},
        )
        transport = FakeWebhookTransport([httpx.Response(200)])
        client = OutboundWebhookClient(registry=registry, transport=transport)

        with self.assertRaises(ValueError):
            await client.send(target="missing", event_type="workflow.failed", payload={})
        self.assertEqual(transport.requests, [])


if __name__ == "__main__":
    unittest.main()
