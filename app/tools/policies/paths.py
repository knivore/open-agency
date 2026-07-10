from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class PathBlockRule:
    patterns: tuple[str, ...]
    reason: str
    path_parts: tuple[str, ...] = ()
    basename_prefixes: tuple[str, ...] = ()


SENSITIVE_PATH_RULES: tuple[PathBlockRule, ...] = (
    PathBlockRule(
        patterns=(".env", ".env.*"),
        reason="reading .env files is blocked",
        basename_prefixes=(".env.",),
    ),
    PathBlockRule(
        patterns=("**/.ssh/**",),
        reason="reading SSH credentials is blocked",
        path_parts=(".ssh",),
    ),
    PathBlockRule(
        patterns=("**/.aws/**",),
        reason="reading AWS credentials is blocked",
        path_parts=(".aws",),
    ),
    PathBlockRule(
        patterns=("**/.git/**",),
        reason="reading git internals is blocked",
        path_parts=(".git",),
    ),
    PathBlockRule(
        patterns=("**/secrets/**",),
        reason="reading secrets directories is blocked",
        path_parts=("secrets",),
    ),
)

EDIT_ONLY_PATH_RULES: tuple[PathBlockRule, ...] = (
    PathBlockRule(
        patterns=("**/node_modules/**",),
        reason="editing node_modules is blocked",
        path_parts=("node_modules",),
    ),
)

_QUOTED_CANDIDATE_PATTERN = re.compile(r"['\"]([^'\"]+)['\"]")
_INLINE_PATH_CANDIDATE_PATTERN = re.compile(
    r"(?:~|\$HOME|/)[^\s'\"`;|&<>()]+"
    r"|(?:[^\s'\"`;|&<>()]+/)?\.env(?:\.[A-Za-z0-9_.-]+)?"
    r"|(?:[^\s'\"`;|&<>()]+/)?(?:\.ssh|\.aws|\.git)(?:/[^\s'\"`;|&<>()]+)?"
    r"|(?:[^\s'\"`;|&<>()]+/)?secrets(?:/[^\s'\"`;|&<>()]+)?"
)


def blocked_path_reason(path: str, *, include_edit_only: bool = False) -> str | None:
    normalized = _normalize_path_candidate(path)
    if not normalized:
        return None
    rules = (*SENSITIVE_PATH_RULES, *(EDIT_ONLY_PATH_RULES if include_edit_only else ()))
    for rule in rules:
        if _matches_rule(normalized, rule):
            return rule.reason
    return None


def path_blocked(path: str, *, include_edit_only: bool = False) -> bool:
    return blocked_path_reason(path, include_edit_only=include_edit_only) is not None


def find_blocked_path_references(command: str) -> list[str]:
    normalized = re.sub(r"[\r\n]+", " ", command.strip())
    if not normalized:
        return []

    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        tokens = normalized.split()

    blocked: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        for candidate in _path_candidates(token):
            if blocked_path_reason(candidate) is None or candidate in seen:
                continue
            seen.add(candidate)
            blocked.append(candidate)
    return blocked


def _path_candidates(token: str) -> list[str]:
    candidates = [token]
    candidates.extend(match.group(1) for match in _QUOTED_CANDIDATE_PATTERN.finditer(token))
    candidates.extend(match.group(0) for match in _INLINE_PATH_CANDIDATE_PATTERN.finditer(token))
    return candidates


def _normalize_path_candidate(path: str) -> str:
    normalized = path.replace("\\", "/").strip().strip("()[]{}<>,:;")
    if normalized.startswith(("'", '"')) and normalized.endswith(("'", '"')) and len(normalized) >= 2:
        normalized = normalized[1:-1]
    return normalized.lstrip("/")


def _matches_rule(normalized_path: str, rule: PathBlockRule) -> bool:
    parts = [part for part in normalized_path.split("/") if part]
    if any(part in rule.path_parts for part in parts):
        return True
    if parts and (parts[-1] == ".env" or any(parts[-1].startswith(prefix) for prefix in rule.basename_prefixes)):
        return True
    return any(
        fnmatch.fnmatch(normalized_path, pattern) or fnmatch.fnmatch(f"/{normalized_path}", pattern)
        for pattern in rule.patterns
    )
