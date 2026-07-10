"""Pydantic schemas for outbound webhook targets and send results."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pydantic import Field, field_validator, model_validator
from typing import Any

from app.core.time import utc_now
from app.domain import DomainModel


class WebhookAuthType(str, Enum):
    NONE = "none"
    BEARER = "bearer"
    HMAC = "hmac"


class WebhookTarget(DomainModel):
    target: str
    url_env: str
    auth_type: WebhookAuthType = WebhookAuthType.NONE
    token_env: str | None = None
    secret_env: str | None = None
    default_headers: dict[str, str] = Field(default_factory=lambda: {"Content-Type": "application/json"})
    timeout_seconds: float = 10
    max_retries: int = 3
    backoff_seconds: float = 0.25

    @field_validator("target", "url_env")
    @classmethod
    def require_non_empty_string(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_auth_env(self) -> "WebhookTarget":
        if self.auth_type == WebhookAuthType.BEARER and not self.token_env:
            raise ValueError("token_env is required for bearer webhook targets")
        if self.auth_type == WebhookAuthType.HMAC and not self.secret_env:
            raise ValueError("secret_env is required for hmac webhook targets")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")
        return self


class WebhookSendResult(DomainModel):
    ok: bool
    target: str
    event_type: str
    status: str
    attempts: int
    idempotency_key: str | None = None
    request_payload_sha256: str
    response_status: int | None = None
    response_body_preview: str | None = None
    error_message: str | None = None
    audit_event_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime = Field(default_factory=utc_now)


class ResolvedWebhookTarget(DomainModel):
    definition: WebhookTarget
    url: str
    token: str | None = None
    secret: str | None = None


WebhookPayload = dict[str, Any]
