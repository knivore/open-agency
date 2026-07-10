from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.core.onecli_http import ONECLI_BLOCKED_HEADER_NAMES, ONECLI_BLOCKED_QUERY_PARAM_NAMES
from app.tools.contracts.models import PolicyRuleResult, PolicyVerdict

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
BLOCKED_HOSTS = {"169.254.169.254", "metadata.google.internal"}


def evaluate_http_request_policy(
        payload: dict[str, Any],
        *,
        allowed_hosts: list[str],
        actor: str | None = None,
) -> PolicyVerdict:
    rules: list[PolicyRuleResult] = []
    parsed_url = urlparse(str(payload.get("url") or ""))
    method = str(payload.get("method") or "").upper()

    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        rules.append(
            PolicyRuleResult(
                id="http-url-scheme",
                outcome="deny",
                reason="url must be an absolute http or https URL",
            )
        )
    else:
        rules.append(PolicyRuleResult(id="http-url-scheme", outcome="ok"))

    host = (parsed_url.hostname or "").lower()
    if host in BLOCKED_HOSTS:
        rules.append(PolicyRuleResult(id="http-host-safety", outcome="deny", reason=f"blocked host: {host}"))
    elif not _host_allowed(host, allowed_hosts):
        rules.append(
            PolicyRuleResult(
                id="http-host-allowlist",
                outcome="deny",
                reason=f"host is not allowlisted: {host}",
            )
        )
    else:
        rules.append(PolicyRuleResult(id="http-host-allowlist", outcome="ok", reason="host is allowlisted"))

    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
        rules.append(PolicyRuleResult(id="http-method", outcome="deny", reason=f"unsupported HTTP method: {method}"))
    elif method in MUTATING_METHODS and not (actor and actor.startswith("approved/")):
        rules.append(
            PolicyRuleResult(
                id="http-mutation-approval-context",
                outcome="warn",
                reason="mutating HTTP request is policy-mediated but actor is not explicitly approved",
            )
        )
    else:
        rules.append(PolicyRuleResult(id="http-method", outcome="ok"))

    if payload.get("verify_ssl") is False:
        rules.append(PolicyRuleResult(id="http-verify-ssl", outcome="warn", reason="TLS verification is disabled"))
    else:
        rules.append(PolicyRuleResult(id="http-verify-ssl", outcome="ok"))

    if str(payload.get("credential_mode") or "none").lower() == "onecli":
        headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
        blocked_headers = sorted(
            name
            for name in (str(key).strip().lower() for key in headers)
            if name in ONECLI_BLOCKED_HEADER_NAMES
        )
        if blocked_headers:
            rules.append(
                PolicyRuleResult(
                    id="http-onecli-no-direct-auth-headers",
                    outcome="deny",
                    reason="OneCLI mode rejects direct credential-bearing headers: " + ", ".join(blocked_headers),
                )
            )
        else:
            rules.append(PolicyRuleResult(id="http-onecli-no-direct-auth-headers", outcome="ok"))

        query_params = payload.get("query_params") if isinstance(payload.get("query_params"), dict) else {}
        blocked_query_params = sorted(
            name
            for name in (str(key).strip().lower() for key in query_params)
            if name in ONECLI_BLOCKED_QUERY_PARAM_NAMES
        )
        if blocked_query_params:
            rules.append(
                PolicyRuleResult(
                    id="http-onecli-no-direct-auth-query",
                    outcome="deny",
                    reason="OneCLI mode rejects direct credential-bearing query parameters: "
                           + ", ".join(blocked_query_params),
                )
            )
        else:
            rules.append(PolicyRuleResult(id="http-onecli-no-direct-auth-query", outcome="ok"))

    score = sum(100 if rule.outcome == "deny" else 25 if rule.outcome == "warn" else 0 for rule in rules)
    return PolicyVerdict(score=score, rules=rules)


def _host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    if not host:
        return False
    if "*" in allowed_hosts:
        return True
    for allowed in allowed_hosts:
        if allowed.startswith("*.") and host.endswith(allowed[1:]):
            return True
        if host == allowed:
            return True
    return False
