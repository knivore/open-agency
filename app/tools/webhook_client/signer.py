"""HMAC signing helpers for outbound webhook requests."""

from __future__ import annotations

import hmac
from hashlib import sha256

from app.core.time import utc_now


def sign_body(secret: str, body: bytes, *, timestamp: str | None = None) -> str:
    signed = body if timestamp is None else timestamp.encode("utf-8") + b"." + body
    return "sha256=" + hmac.new(secret.encode("utf-8"), signed, sha256).hexdigest()


def build_hmac_headers(secret: str, body: bytes, *, timestamp: str | None = None) -> dict[str, str]:
    resolved_timestamp = timestamp or utc_now().isoformat()
    return {
        "X-Agency-Webhook-Timestamp": resolved_timestamp,
        "X-Agency-Webhook-Signature": sign_body(secret, body, timestamp=resolved_timestamp),
    }
