from __future__ import annotations

import re
from typing import Any

from app.tools.contracts.models import PolicyRuleResult, PolicyVerdict
from app.tools.policies.paths import blocked_path_reason, find_blocked_path_references

BLOCKED_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|[;&|]\s*)sudo(\s|$)", re.IGNORECASE), "sudo is blocked for agent command execution"),
    (re.compile(r"(^|[;&|]\s*)su(\s|$)", re.IGNORECASE), "switching users is blocked for agent command execution"),
    (re.compile(r"(^|[;&|]\s*)git\s+push(\s|$)", re.IGNORECASE),
     "git push is blocked; pushes require a separate explicit workflow"),
    (re.compile(r"(^|[;&|]\s*)ssh(\s|$)", re.IGNORECASE),
     "ssh is blocked because it can expose credentials or remote systems"),
    (re.compile(r"(^|[;&|]\s*)scp(\s|$)", re.IGNORECASE),
     "scp is blocked because it can expose credentials or remote systems"),
    (
        re.compile(r"(curl|wget)[^;&|]*\|\s*(sudo\s+)?(bash|sh)\b", re.IGNORECASE),
        "piping downloaded scripts into a shell is blocked",
    ),
    (
        re.compile(r"(^|[;&|]\s*)rm\s+(-[^\s]*[rf][^\s]*|-[^\s]*[fr][^\s]*)\s+(/|\$HOME|~)(\s|$)", re.IGNORECASE),
        "broad recursive deletion is blocked",
    ),
    (re.compile(r"(^|[;&|]\s*)find\s+[^;&|]*\s+-delete(\s|$|[;&|])", re.IGNORECASE),
     "find -delete is blocked; use an explicit approved delete workflow"),
    (
        re.compile(r"(^|[;&|]\s*)chmod\s+-R\s+[^;&|]*\s+(/|\$HOME|~)(\s|$)", re.IGNORECASE),
        "recursive chmod on root or home is blocked",
    ),
    (
        re.compile(r"(^|[;&|]\s*)chown\s+-R\s+[^;&|]*\s+(/|\$HOME|~)(\s|$)", re.IGNORECASE),
        "recursive chown on root or home is blocked",
    ),
)


def blocked_command_reason(command: str) -> str | None:
    normalized = re.sub(r"[\r\n]+", ";", command.strip())
    # The shell tool currently relies on command-text inspection rather than a filesystem sandbox.
    # Deny any sensitive path reference conservatively so alternate binaries or interpreter wrappers
    # cannot bypass the policy by spelling the same secret read differently.
    blocked_paths = find_blocked_path_references(normalized)
    if blocked_paths:
        return blocked_path_reason(blocked_paths[0])
    for pattern, reason in BLOCKED_COMMAND_PATTERNS:
        if pattern.search(normalized):
            return reason
    return None


def evaluate_command_run_policy(payload: dict[str, Any], *, actor: str | None = None) -> PolicyVerdict:
    rules: list[PolicyRuleResult] = []
    command = str(payload.get("command") or "").strip()
    if not command:
        rules.append(PolicyRuleResult(id="command-required", outcome="deny", reason="command is required"))
    else:
        rules.append(PolicyRuleResult(id="command-required", outcome="ok"))

    blocked_reason = blocked_command_reason(command) if command else None
    if blocked_reason:
        rules.append(PolicyRuleResult(id="command-safety", outcome="deny", reason=blocked_reason))
    else:
        rules.append(PolicyRuleResult(id="command-safety", outcome="ok"))

    if actor and actor.startswith("approved/"):
        rules.append(PolicyRuleResult(id="command-approval-context", outcome="ok", reason="actor is approved"))
    else:
        rules.append(
            PolicyRuleResult(
                id="command-approval-context",
                outcome="deny",
                reason="command execution requires an explicitly approved actor context",
            )
        )

    score = sum(100 if rule.outcome == "deny" else 25 if rule.outcome == "warn" else 0 for rule in rules)
    return PolicyVerdict(score=score, rules=rules)
