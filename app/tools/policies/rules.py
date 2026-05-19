from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from app.tools.contracts.models import PolicyRuleResult, PolicyVerdict

BLOCKED_PATH_PATTERNS = (
    ".env",
    ".env.*",
    "**/secrets/**",
    "**/.ssh/**",
    "**/node_modules/**",
    "**/.git/**",
)
DENY_SECRET_PATTERNS = (
    re.compile(r"OPENAI_API_KEY\s*=", re.IGNORECASE),
    re.compile(r"AWS_SECRET_ACCESS_KEY\s*=", re.IGNORECASE),
    re.compile(r"PRIVATE KEY", re.IGNORECASE),
)
WARN_SECRET_PATTERNS = (
    re.compile(r"password\s*=", re.IGNORECASE),
    re.compile(r"token\s*=", re.IGNORECASE),
    re.compile(r"secret\s*=", re.IGNORECASE),
)


def evaluate_sandbox_edit_policy(
        payload: dict[str, Any],
        *,
        allowed_repos: list[str],
        actor: str | None = None,
) -> PolicyVerdict:
    rules: list[PolicyRuleResult] = []
    rules.extend(_repo_allowlist(payload, allowed_repos))
    rules.extend(_dangerous_paths(payload))
    rules.extend(_likely_secrets(payload))
    rules.extend(_dry_run_first(payload, actor))
    score = sum(100 if rule.outcome == "deny" else 25 if rule.outcome == "warn" else 0 for rule in rules)
    return PolicyVerdict(score=score, rules=rules)


def _repo_allowlist(payload: dict[str, Any], allowed_repos: list[str]) -> list[PolicyRuleResult]:
    repo = str(payload.get("repo") or "")
    try:
        resolved = str(Path(repo).expanduser().resolve())
    except Exception:
        resolved = repo
    normalized = {str(Path(item).expanduser().resolve()) for item in allowed_repos}
    if resolved not in normalized:
        return [
            PolicyRuleResult(
                id="repo-allowlist",
                outcome="deny",
                reason=f"repo is not allowlisted: {repo}",
            )
        ]
    return [PolicyRuleResult(id="repo-allowlist", outcome="ok", reason="repo is allowlisted")]


def _dangerous_paths(payload: dict[str, Any]) -> list[PolicyRuleResult]:
    denied: list[str] = []
    for change in payload.get("changes") or []:
        path = str(change.get("path") or "")
        if _path_blocked(path):
            denied.append(path)
    if denied:
        return [
            PolicyRuleResult(
                id="no-dangerous-paths",
                outcome="deny",
                reason=f"blocked paths: {', '.join(sorted(denied))}",
            )
        ]
    return [PolicyRuleResult(id="no-dangerous-paths", outcome="ok")]


def _path_blocked(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part]
    if any(part in {".git", ".ssh", "node_modules", "secrets"} for part in parts):
        return True
    if parts and (parts[-1] == ".env" or parts[-1].startswith(".env.")):
        return True
    return any(fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(f"/{normalized}", pattern)
               for pattern in BLOCKED_PATH_PATTERNS)


def _likely_secrets(payload: dict[str, Any]) -> list[PolicyRuleResult]:
    deny_hits: list[str] = []
    warn_hits: list[str] = []
    for change in payload.get("changes") or []:
        path = str(change.get("path") or "<unknown>")
        patch = str(change.get("patch") or "")
        if any(pattern.search(patch) for pattern in DENY_SECRET_PATTERNS):
            deny_hits.append(path)
        elif any(pattern.search(patch) for pattern in WARN_SECRET_PATTERNS):
            warn_hits.append(path)
    if deny_hits:
        return [
            PolicyRuleResult(
                id="no-secrets",
                outcome="deny",
                reason=f"possible high-risk secret in: {', '.join(sorted(set(deny_hits)))}",
            )
        ]
    if warn_hits:
        return [
            PolicyRuleResult(
                id="no-secrets",
                outcome="warn",
                reason=f"possible generic secret/token text in: {', '.join(sorted(set(warn_hits)))}",
            )
        ]
    return [PolicyRuleResult(id="no-secrets", outcome="ok")]


def _dry_run_first(payload: dict[str, Any], actor: str | None) -> list[PolicyRuleResult]:
    if payload.get("dryRun", True):
        return [PolicyRuleResult(id="dry-run-first", outcome="ok")]
    if actor and actor.startswith("approved/"):
        return [PolicyRuleResult(id="dry-run-first", outcome="warn", reason="non-dry-run actor is approved")]
    return [
        PolicyRuleResult(
            id="dry-run-first",
            outcome="deny",
            reason="sandbox-edit must run as dryRun unless actor is explicitly approved",
        )
    ]
