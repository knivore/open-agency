"""Authenticated client used by Agency tools; no engine object leaves the service."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

import httpx

from .contracts import (
    ActionRequest,
    BrowserAction,
    BrowserOptions,
    BrowserRuntimePolicy,
    ExtractMode,
    ExtractRequest,
    OpenRequest,
    OwnerClaims,
)
from .security import derive_execution_secret, issue_capability


class BrowserRuntimeClientError(RuntimeError):
    pass


class BrowserRuntimeClient:
    def __init__(
            self,
            *,
            base_url: str | None = None,
            signing_secret: str | None = None,
            timeout_seconds: float | None = None,
            transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("BROWSER_RUNTIME_URL", "http://127.0.0.1:8010")).rstrip("/")
        self.signing_secret = signing_secret or os.getenv("BROWSER_RUNTIME_SIGNING_SECRET")
        self.execution_secret = os.getenv("BROWSER_RUNTIME_EXECUTION_SECRET")
        self.timeout_seconds = timeout_seconds or float(os.getenv("BROWSER_RUNTIME_CLIENT_TIMEOUT_SECONDS", "120"))
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds, transport=transport)

    def open(
            self,
            *,
            url: str,
            owner: OwnerClaims | dict[str, Any],
            goal: str | None = None,
            extract_mode: ExtractMode = "auto",
            keep_open: bool = False,
            session_id: str | None = None,
            allowed_hosts: list[str] | None = None,
            options: BrowserOptions | dict[str, Any] | None = None,
            runtime_policy: BrowserRuntimePolicy | dict[str, Any] | None = None,
            correlation_id: str | None = None,
    ) -> dict[str, Any]:
        owner_claims = OwnerClaims.model_validate(owner)
        request_hosts = list(allowed_hosts or [])
        explicit_host = urlsplit(url).hostname
        if explicit_host and explicit_host not in request_hosts:
            # Each explicit navigation grants only its public origin for this
            # one capability; it never turns into a wildcard host grant.
            request_hosts.append(explicit_host)
        request = OpenRequest(
            url=url,
            goal=goal,
            extract_mode=extract_mode,
            keep_open=keep_open,
            session_id=session_id,
            allowed_hosts=request_hosts,
            options=BrowserOptions.model_validate(options or {}),
            runtime_policy=BrowserRuntimePolicy.model_validate(runtime_policy or {}),
            correlation_id=correlation_id,
        )
        return self._request("POST", "/v1/open", operation="open", owner=owner_claims,
                             allowed_hosts=request.allowed_hosts, json=request.model_dump(mode="json"))

    def extract(
            self,
            session_id: str,
            *,
            owner: OwnerClaims | dict[str, Any],
            extract_mode: ExtractMode = "auto",
            goal: str | None = None,
            max_chars: int = 100_000,
            correlation_id: str | None = None,
    ) -> dict[str, Any]:
        request = ExtractRequest(extract_mode=extract_mode, goal=goal, max_chars=max_chars,
                                 correlation_id=correlation_id)
        return self._request("POST", f"/v1/sessions/{session_id}/extract", operation="extract",
                             owner=OwnerClaims.model_validate(owner), json=request.model_dump(mode="json"))

    def action(
            self,
            session_id: str,
            *,
            owner: OwnerClaims | dict[str, Any],
            action: BrowserAction,
            **payload: Any,
    ) -> dict[str, Any]:
        request = ActionRequest(action=action, **payload)
        return self._request("POST", f"/v1/sessions/{session_id}/actions", operation="action",
                             owner=OwnerClaims.model_validate(owner), json=request.model_dump(mode="json"))

    def close(self, session_id: str, *, owner: OwnerClaims | dict[str, Any]) -> dict[str, Any]:
        return self._request("DELETE", f"/v1/sessions/{session_id}", operation="close",
                             owner=OwnerClaims.model_validate(owner))

    def status(self, *, owner: OwnerClaims | dict[str, Any]) -> dict[str, Any]:
        return self._request("GET", "/v1/sessions", operation="status", owner=OwnerClaims.model_validate(owner))

    def close_execution(self, execution_id: str, *, owner: OwnerClaims | dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/v1/executions/{execution_id}/sessions",
            operation="close_execution",
            owner=OwnerClaims.model_validate(owner),
        )

    def health(self) -> dict[str, Any]:
        try:
            response = self._client.get("/health")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BrowserRuntimeClientError(f"Browser runtime health request failed: {exc}") from exc

    def close_client(self) -> None:
        self._client.close()

    def _request(
            self,
            method: str,
            path: str,
            *,
            operation: str,
            owner: OwnerClaims,
            allowed_hosts: list[str] | None = None,
            json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        secret = self.execution_secret
        if not secret and self.signing_secret and owner.execution_id:
            secret = derive_execution_secret(self.signing_secret, owner.execution_id)
        secret = secret or self.signing_secret
        if not secret:
            raise BrowserRuntimeClientError(
                "BROWSER_RUNTIME_EXECUTION_SECRET or BROWSER_RUNTIME_SIGNING_SECRET is required"
            )
        token = issue_capability(
            secret,
            owner=owner,
            operations=[operation],
            allowed_hosts=allowed_hosts or [],
        )
        try:
            response = self._client.request(method, path, json=json, headers={"Authorization": f"Bearer {token}"})
            if response.is_error:
                try:
                    detail = response.json().get("detail")
                except ValueError:
                    detail = response.text[:500]
                raise BrowserRuntimeClientError(f"Browser runtime returned HTTP {response.status_code}: {detail}")
            return response.json()
        except BrowserRuntimeClientError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise BrowserRuntimeClientError(f"Browser runtime request failed: {exc}") from exc

