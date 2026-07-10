"""Async outbound webhook client with retries and audit persistence."""

from __future__ import annotations

import asyncio
import hashlib
import httpx
from typing import Any, Protocol
from urllib.parse import urlparse

from app.domain import ExecutionEventType, OutboundWebhookAttempt
from app.runtime.events.factory import RuntimeEventEnvelope, RuntimeEventStatus, \
    create_execution_event_from_runtime_event
from app.runtime.events.payloads import canonical_json, payload_sha256
from .registry import WebhookTargetRegistry
from .schemas import ResolvedWebhookTarget, WebhookAuthType, WebhookPayload, WebhookSendResult
from .signer import build_hmac_headers


class AsyncWebhookTransport(Protocol):
    async def post(
            self,
            url: str,
            *,
            content: bytes,
            headers: dict[str, str],
            timeout: float,
    ) -> httpx.Response: ...


RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
MAX_RESPONSE_PREVIEW_CHARS = 500


class OutboundWebhookClient:
    def __init__(
            self,
            *,
            registry: WebhookTargetRegistry,
            execution_store: Any | None = None,
            attempt_repository: Any | None = None,
            transport: AsyncWebhookTransport | None = None,
    ):
        self.registry = registry
        self.execution_store = execution_store
        self.attempt_repository = attempt_repository
        self.transport = transport

    async def send(
            self,
            *,
            target: str,
            event_type: str,
            payload: WebhookPayload,
            idempotency_key: str | None = None,
            headers: dict[str, str] | None = None,
            run_id: str | None = None,
            workflow_id: str | None = None,
    ) -> WebhookSendResult:
        resolved = self.registry.resolve(target)
        body_payload = {"event_type": event_type, "payload": payload}
        body = self._body_bytes(body_payload)
        request_hash = payload_sha256(body_payload)
        audit_run_id = run_id or self._string_value(payload.get("run_id")) or self._string_value(
            payload.get("execution_id")
        )
        audit_workflow_id = workflow_id or self._string_value(payload.get("workflow_id"))
        audit_event_ids: list[str] = []
        queued_event_id = await self._audit(
            event_type=ExecutionEventType.OUTBOUND_WEBHOOK_QUEUED,
            status=RuntimeEventStatus.QUEUED,
            run_id=audit_run_id,
            workflow_id=audit_workflow_id,
            payload={
                "target": target,
                "event_type": event_type,
                "idempotency_key": idempotency_key,
                "request_payload_sha256": request_hash,
                "url_hash": self._url_hash(resolved.url),
            },
        )
        if queued_event_id:
            audit_event_ids.append(queued_event_id)

        attempts = 0
        last_response_status: int | None = None
        last_response_preview: str | None = None
        last_error: str | None = None
        max_attempts = resolved.definition.max_retries + 1

        async def _send_with_transport(transport: AsyncWebhookTransport) -> httpx.Response:
            request_headers = self._headers_for(
                resolved,
                body,
                extra_headers=headers,
                idempotency_key=idempotency_key,
            )
            return await transport.post(
                resolved.url,
                content=body,
                headers=request_headers,
                timeout=resolved.definition.timeout_seconds,
            )

        close_client = False
        transport = self.transport
        if transport is None:
            transport = httpx.AsyncClient()
            close_client = True

        try:
            for attempt_no in range(1, max_attempts + 1):
                attempts = attempt_no
                try:
                    response = await _send_with_transport(transport)
                    last_response_status = response.status_code
                    last_response_preview = self._sanitize_sensitive_text(
                        self._response_preview(response),
                        resolved=resolved,
                    )
                    last_error = None
                    attempt_status = "sent" if 200 <= response.status_code < 300 else "failed"
                    attempt_error = None if attempt_status == "sent" else f"HTTP {response.status_code}"
                    await self._record_attempt(
                        event_id=queued_event_id,
                        target=target,
                        url_hash=self._url_hash(resolved.url),
                        idempotency_key=idempotency_key,
                        request_payload_sha256=request_hash,
                        response_status=response.status_code,
                        response_body_preview=last_response_preview,
                        attempt_no=attempt_no,
                        status=attempt_status,
                        error_message=attempt_error,
                    )
                    if 200 <= response.status_code < 300:
                        sent_event_id = await self._audit(
                            event_type=ExecutionEventType.OUTBOUND_WEBHOOK_SENT,
                            status=RuntimeEventStatus.COMPLETED,
                            run_id=audit_run_id,
                            workflow_id=audit_workflow_id,
                            payload={
                                "target": target,
                                "event_type": event_type,
                                "idempotency_key": idempotency_key,
                                "request_payload_sha256": request_hash,
                                "url_hash": self._url_hash(resolved.url),
                                "attempt_no": attempt_no,
                                "response_status": response.status_code,
                                "response_body_preview": last_response_preview,
                            },
                        )
                        if sent_event_id:
                            audit_event_ids.append(sent_event_id)
                        return WebhookSendResult(
                            ok=True,
                            target=target,
                            event_type=event_type,
                            status="sent",
                            attempts=attempts,
                            idempotency_key=idempotency_key,
                            request_payload_sha256=request_hash,
                            response_status=last_response_status,
                            response_body_preview=last_response_preview,
                            audit_event_ids=audit_event_ids,
                        )
                    last_error = f"HTTP {response.status_code}"
                    if response.status_code not in RETRYABLE_STATUS_CODES:
                        break
                except Exception as exc:
                    last_error = self._sanitize_sensitive_text(str(exc), resolved=resolved)
                    last_response_status = None
                    last_response_preview = None
                    await self._record_attempt(
                        event_id=queued_event_id,
                        target=target,
                        url_hash=self._url_hash(resolved.url),
                        idempotency_key=idempotency_key,
                        request_payload_sha256=request_hash,
                        response_status=None,
                        response_body_preview=None,
                        attempt_no=attempt_no,
                        status="failed",
                        error_message=last_error,
                    )

                if attempt_no < max_attempts:
                    await asyncio.sleep(resolved.definition.backoff_seconds * attempt_no)
        finally:
            if close_client:
                await transport.aclose()  # type: ignore[attr-defined]

        failed_event_id = await self._audit(
            event_type=ExecutionEventType.OUTBOUND_WEBHOOK_FAILED,
            status=RuntimeEventStatus.FAILED,
            run_id=audit_run_id,
            workflow_id=audit_workflow_id,
            payload={
                "target": target,
                "event_type": event_type,
                "idempotency_key": idempotency_key,
                "request_payload_sha256": request_hash,
                "url_hash": self._url_hash(resolved.url),
                "attempt_no": attempts,
                "response_status": last_response_status,
                "response_body_preview": last_response_preview,
                "error_message": last_error,
            },
        )
        if failed_event_id:
            audit_event_ids.append(failed_event_id)
        return WebhookSendResult(
            ok=False,
            target=target,
            event_type=event_type,
            status="failed",
            attempts=attempts,
            idempotency_key=idempotency_key,
            request_payload_sha256=request_hash,
            response_status=last_response_status,
            response_body_preview=last_response_preview,
            error_message=last_error,
            audit_event_ids=audit_event_ids,
        )

    def _headers_for(
            self,
            resolved: ResolvedWebhookTarget,
            body: bytes,
            *,
            extra_headers: dict[str, str] | None,
            idempotency_key: str | None,
    ) -> dict[str, str]:
        headers = {**resolved.definition.default_headers, **(extra_headers or {})}
        headers.setdefault("Content-Type", "application/json")
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if resolved.definition.auth_type == WebhookAuthType.BEARER:
            headers["Authorization"] = f"Bearer {resolved.token}"
        if resolved.definition.auth_type == WebhookAuthType.HMAC:
            headers.update(build_hmac_headers(resolved.secret or "", body))
        return headers

    async def _audit(
            self,
            *,
            event_type: ExecutionEventType,
            status: RuntimeEventStatus,
            run_id: str | None,
            workflow_id: str | None,
            payload: dict[str, Any],
    ) -> str | None:
        if self.execution_store is None or not run_id:
            return None
        try:
            envelope = RuntimeEventEnvelope(
                event_type=event_type,
                run_id=run_id,
                workflow_id=workflow_id,
                source="outbound_webhook_client",
                status=status,
                payload=payload,
            )
            event = create_execution_event_from_runtime_event(envelope)
            saved = await self.execution_store.save_event(event)
            return saved.id
        except Exception:
            return None

    async def _record_attempt(
            self,
            *,
            event_id: str | None,
            target: str,
            url_hash: str,
            idempotency_key: str | None,
            request_payload_sha256: str,
            response_status: int | None,
            response_body_preview: str | None,
            attempt_no: int,
            status: str,
            error_message: str | None,
    ) -> None:
        if self.attempt_repository is None:
            return
        attempt = OutboundWebhookAttempt(
            event_id=event_id,
            target=target,
            url_hash=url_hash,
            idempotency_key=idempotency_key,
            request_payload_sha256=request_payload_sha256,
            response_status=response_status,
            response_body_preview=response_body_preview,
            attempt_no=attempt_no,
            status=status,
            error_message=error_message,
        )
        try:
            await self.attempt_repository.create_attempt(attempt)
        except Exception:
            return

    def _body_bytes(self, body_payload: dict[str, Any]) -> bytes:
        return canonical_json(body_payload).encode("utf-8")

    def _response_preview(self, response: httpx.Response) -> str:
        try:
            text = response.text
        except Exception:
            text = ""
        return text[:MAX_RESPONSE_PREVIEW_CHARS]

    def _sanitize_sensitive_text(self, message: str, *, resolved: ResolvedWebhookTarget) -> str:
        sanitized = message.replace(resolved.url, "[REDACTED_URL]")
        parsed = urlparse(resolved.url)
        for fragment in (parsed.netloc, parsed.path, *[part for part in parsed.path.split("/") if part]):
            if fragment:
                sanitized = sanitized.replace(fragment, "[REDACTED_URL]")
        if resolved.token:
            sanitized = sanitized.replace(resolved.token, "[REDACTED_TOKEN]")
        if resolved.secret:
            sanitized = sanitized.replace(resolved.secret, "[REDACTED_SECRET]")
        return sanitized[:MAX_RESPONSE_PREVIEW_CHARS]

    def _url_hash(self, url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _string_value(self, value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None
