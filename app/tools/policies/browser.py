from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.core.outbound_http import validate_outbound_http_url
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
                # The executor only stops on deny, so approval cannot be advisory
                # for interactions that may submit forms or trigger remote actions.
                outcome="deny",
                reason="browser mutation requires an explicitly approved actor context",
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
    try:
        validate_outbound_http_url(url, allowed_hosts=allowed_hosts)
    except ValueError as exc:
        return [
            PolicyRuleResult(
                id="browser-host-allowlist",
                outcome="deny",
                reason=str(exc),
            )
        ]
    return [PolicyRuleResult(id="browser-host-allowlist", outcome="ok", reason="host is allowlisted")]
