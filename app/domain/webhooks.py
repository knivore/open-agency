"""Domain records for outbound webhook delivery auditing."""

from __future__ import annotations

from datetime import datetime
from pydantic import Field
from uuid import uuid4

from app.core.time import utc_now
from .credentials import DomainModel


class OutboundWebhookAttempt(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    event_id: str | None = None
    target: str
    url_hash: str
    idempotency_key: str | None = None
    request_payload_sha256: str
    response_status: int | None = None
    response_body_preview: str | None = None
    attempt_no: int
    status: str
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
