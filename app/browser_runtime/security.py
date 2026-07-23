"""Capability authentication and fail-closed browser destination policy."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .contracts import OwnerClaims


class BrowserCapabilityError(PermissionError):
    pass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True, slots=True)
class BrowserCapability:
    audience: str
    expires_at: int
    issued_at: int
    nonce: str
    owner: OwnerClaims
    operations: tuple[str, ...]
    allowed_hosts: tuple[str, ...]


def derive_execution_secret(master_secret: str, execution_id: str) -> str:
    """Limit a worker's signing authority to its own execution identity."""

    if len(master_secret) < 32:
        raise BrowserCapabilityError("Browser runtime master secret must contain at least 32 characters")
    return _b64encode(hmac.new(
        master_secret.encode("utf-8"),
        f"agency-browser-execution:{execution_id}".encode("utf-8"),
        hashlib.sha256,
    ).digest())


def peek_capability_owner(token: str) -> OwnerClaims:
    """Read only the owner selector used to choose a derived verification key."""

    try:
        encoded, _ = token.split(".", 1)
        payload = json.loads(_b64decode(encoded))
        return OwnerClaims.model_validate(payload["owner"])
    except Exception as exc:
        raise BrowserCapabilityError("Malformed browser capability") from exc


def issue_capability(
        secret: str,
        *,
        owner: OwnerClaims,
        operations: list[str],
        allowed_hosts: list[str],
        ttl_seconds: int = 30,
        now: int | None = None,
) -> str:
    if len(secret) < 32:
        raise BrowserCapabilityError("Browser runtime signing secret must contain at least 32 characters")
    if not owner.is_identified:
        raise BrowserCapabilityError("Browser capabilities require an execution or authenticated actor owner")
    issued_at = int(time.time() if now is None else now)
    payload = {
        "aud": "agency-browser-runtime",
        "iat": issued_at,
        "exp": issued_at + max(1, min(ttl_seconds, 300)),
        "jti": secrets.token_urlsafe(18),
        "owner": owner.model_dump(exclude_none=True),
        "operations": sorted(set(operations)),
        "allowed_hosts": sorted({host.lower().rstrip(".") for host in allowed_hosts}),
    }
    encoded = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _b64encode(hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_capability(
        secret: str,
        token: str,
        *,
        operation: str,
        now: int | None = None,
) -> BrowserCapability:
    try:
        encoded, supplied = token.split(".", 1)
        expected = _b64encode(hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied, expected):
            raise BrowserCapabilityError("Invalid browser capability signature")
        payload: dict[str, Any] = json.loads(_b64decode(encoded))
    except BrowserCapabilityError:
        raise
    except Exception as exc:
        raise BrowserCapabilityError("Malformed browser capability") from exc
    current = int(time.time() if now is None else now)
    if payload.get("aud") != "agency-browser-runtime":
        raise BrowserCapabilityError("Invalid browser capability audience")
    if current < int(payload.get("iat", 0)) - 30 or current >= int(payload.get("exp", 0)):
        raise BrowserCapabilityError("Browser capability is expired or not yet valid")
    operations = tuple(str(value) for value in payload.get("operations", []))
    if operation not in operations:
        raise BrowserCapabilityError(f"Browser capability does not allow '{operation}'")
    return BrowserCapability(
        audience="agency-browser-runtime",
        issued_at=int(payload["iat"]),
        expires_at=int(payload["exp"]),
        nonce=str(payload["jti"]),
        owner=OwnerClaims.model_validate(payload["owner"]),
        operations=operations,
        allowed_hosts=tuple(str(value) for value in payload.get("allowed_hosts", [])),
    )


def validate_public_url(url: str, allowed_hosts: list[str] | tuple[str, ...]) -> str:
    """Validate URL syntax, host authorization, and every current DNS answer.

    The browser route handler invokes this again for redirects and subresources;
    checking all answers limits simple DNS-rebinding and mixed-answer bypasses.
    """

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute HTTP(S) browser URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("Browser URLs must not contain credentials")
    host = parsed.hostname.lower().rstrip(".")
    approved = {item.lower().rstrip(".") for item in allowed_hosts}
    if host not in approved:
        raise ValueError(f"Browser host '{host}' is not approved for this operation")
    try:
        answers = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Browser host '{host}' could not be resolved") from exc
    addresses = {entry[4][0] for entry in answers}
    if not addresses:
        raise ValueError(f"Browser host '{host}' returned no DNS addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError(f"Browser host '{host}' resolves to a non-public address")
    return url

