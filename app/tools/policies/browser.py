from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.tools.contracts.models import PolicyRuleResult, PolicyVerdict
from .http import BLOCKED_HOSTS

BROWSER_MUTATION_TOOLS = {
    "agency.browser.click",
    "agency.browser.select-option",
    "agency.browser.type-text",
}


def evaluate_browser_policy(
        tool_name: str,
        payload: dict[str, Any],
        *,
        allowed_hosts: list[str],
        actor: str | None = None,
) -> PolicyVerdict:
    rules: list[PolicyRuleResult] = []
    if tool_name == "agency.browser.open":
        rules.extend(_evaluate_browser_open_url(payload, allowed_hosts=allowed_hosts))
    else:
        rules.append(PolicyRuleResult(id="browser-session-context", outcome="ok"))

    if tool_name in BROWSER_MUTATION_TOOLS and not (actor and actor.startswith("approved/")):
        rules.append(
            PolicyRuleResult(
                id="browser-mutation-approval-context",
                outcome="warn",
                reason="browser mutation is policy-mediated but actor is not explicitly approved",
            )
        )
    else:
        rules.append(PolicyRuleResult(id="browser-mutation-approval-context", outcome="ok"))

    score = sum(100 if rule.outcome == "deny" else 25 if rule.outcome == "warn" else 0 for rule in rules)
    return PolicyVerdict(score=score, rules=rules)


def _evaluate_browser_open_url(payload: dict[str, Any], *, allowed_hosts: list[str]) -> list[PolicyRuleResult]:
    url = str(payload.get("url") or "")
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        return [
            PolicyRuleResult(
                id="browser-url-scheme",
                outcome="deny",
                reason="url must be an absolute http or https URL",
            )
        ]

    host = (parsed_url.hostname or "").lower()
    if host in BLOCKED_HOSTS:
        return [PolicyRuleResult(id="browser-host-safety", outcome="deny", reason=f"blocked host: {host}")]
    if not _host_allowed(host, allowed_hosts):
        return [
            PolicyRuleResult(
                id="browser-host-allowlist",
                outcome="deny",
                reason=f"host is not allowlisted: {host}",
            )
        ]
    return [PolicyRuleResult(id="browser-host-allowlist", outcome="ok", reason="host is allowlisted")]


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
