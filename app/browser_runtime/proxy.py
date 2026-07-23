"""Credential-bound proxy resolution and bounded per-domain stickiness."""

from __future__ import annotations

import json
import hashlib
import os
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit, urlunsplit


class ProxyConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedProxy:
    server: str
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    binding: str | None = None

    def playwright_options(self) -> dict[str, str]:
        return {
            "server": self.server,
            **({"username": self.username} if self.username else {}),
            **({"password": self.password} if self.password else {}),
        }

@dataclass(slots=True)
class _StickyAssignment:
    binding: str
    expires_at: float
    remaining_requests: int


class ProxyResolver:
    """Resolve opaque Agency bindings; raw proxy URLs never enter tool payloads."""

    def __init__(self, bindings: dict[str, str | list[str]] | None = None, *, clock=time.time) -> None:
        if bindings is None:
            raw = os.getenv("BROWSER_RUNTIME_PROXY_BINDINGS_JSON", "{}")
            try:
                bindings = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ProxyConfigurationError("BROWSER_RUNTIME_PROXY_BINDINGS_JSON must be valid JSON") from exc
        if not isinstance(bindings, dict):
            raise ProxyConfigurationError("Browser proxy bindings must be a JSON object")
        normalized_bindings: dict[str, str | list[str]] = {}
        for key, value in bindings.items():
            if not isinstance(key, str) or not (
                    isinstance(value, str)
                    or isinstance(value, list) and all(isinstance(item, str) for item in value)
            ):
                raise ProxyConfigurationError("Browser proxy bindings must map names to endpoint strings or lists")
            normalized_bindings[key] = value
        self._bindings = normalized_bindings
        self._sticky: dict[str, _StickyAssignment] = {}
        self._rotation: dict[str, int] = {}
        self._clock = clock
        self.sticky_ttl_seconds = max(1, int(os.getenv("BROWSER_PROXY_STICKY_TTL_SECONDS", "600")))
        self.sticky_request_count = max(1, int(os.getenv("BROWSER_PROXY_STICKY_REQUEST_COUNT", "20")))

    def resolve(self, binding: str | None, *, domain: str) -> ResolvedProxy | None:
        now = self._clock()
        assignment = self._sticky.get(domain)
        if assignment and assignment.expires_at > now and assignment.remaining_requests > 0 and binding in {None, assignment.binding}:
            binding = assignment.binding
            assignment.remaining_requests -= 1
        elif assignment:
            self._sticky.pop(domain, None)
        if not binding or binding == "direct":
            return None
        configured = self._bindings.get(binding)
        if not configured:
            raise ProxyConfigurationError(f"Unknown browser proxy binding '{binding}'")
        pool = configured if isinstance(configured, list) else [configured]
        if not pool or not all(isinstance(item, str) and item for item in pool):
            raise ProxyConfigurationError(f"Proxy binding '{binding}' must contain one or more endpoints")
        stable_index = int(hashlib.sha256(domain.encode("utf-8")).hexdigest(), 16) % len(pool)
        raw = pool[(stable_index + self._rotation.get(domain, 0)) % len(pool)]
        resolved = self._normalize(raw, binding=binding)
        self._sticky[domain] = _StickyAssignment(
            binding=binding,
            expires_at=now + self.sticky_ttl_seconds,
            remaining_requests=self.sticky_request_count - 1,
        )
        return resolved

    def invalidate(self, domain: str) -> None:
        self._sticky.pop(domain, None)
        self._rotation[domain] = self._rotation.get(domain, 0) + 1

    @staticmethod
    def _normalize(raw: str, *, binding: str) -> ResolvedProxy:
        parsed = urlsplit(raw if "://" in raw else f"http://{raw}")
        if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or not parsed.port:
            raise ProxyConfigurationError(f"Proxy binding '{binding}' has an invalid endpoint")
        server = urlunsplit((parsed.scheme, f"{parsed.hostname}:{parsed.port}", "", "", ""))
        return ResolvedProxy(
            server=server,
            username=parsed.username,
            password=parsed.password,
            binding=binding,
        )

    @staticmethod
    def authenticated_url(proxy: ResolvedProxy) -> str:
        if not proxy.username:
            return proxy.server
        parsed = urlsplit(proxy.server)
        auth = quote(proxy.username, safe="")
        if proxy.password:
            auth += ":" + quote(proxy.password, safe="")
        return urlunsplit((parsed.scheme, f"{auth}@{parsed.netloc}", "", "", ""))

