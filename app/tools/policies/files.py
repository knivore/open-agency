from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.tools.contracts.models import PolicyRuleResult, PolicyVerdict
from app.tools.policies.paths import path_blocked

DENY_CONTENT_PATTERNS = (
    re.compile(r"OPENAI_API_KEY\s*=", re.IGNORECASE),
    re.compile(r"AWS_SECRET_ACCESS_KEY\s*=", re.IGNORECASE),
    re.compile(r"PRIVATE KEY", re.IGNORECASE),
)


def evaluate_file_write_text_policy(
        payload: dict[str, Any],
        *,
        allowed_dirs: list[str],
        actor: str | None = None,
) -> PolicyVerdict:
    rules: list[PolicyRuleResult] = []
    base_folder = str(payload.get("base_folder") or "")
    filename = str(payload.get("filename") or "")
    target_path = _resolve_target_path(base_folder, filename)

    if not base_folder or not filename:
        rules.append(
            PolicyRuleResult(
                id="file-target-required",
                outcome="deny",
                reason="base_folder and filename are required for contract-backed file writes",
            )
        )
    else:
        rules.append(PolicyRuleResult(id="file-target-required", outcome="ok"))

    rules.extend(_allowed_directory(target_path, allowed_dirs))
    rules.extend(_dangerous_file_path(filename))
    rules.extend(_likely_secret_content(payload))

    if actor and actor.startswith("approved/"):
        rules.append(PolicyRuleResult(id="file-write-approval-context", outcome="ok", reason="actor is approved"))
    else:
        rules.append(
            PolicyRuleResult(
                id="file-write-approval-context",
                outcome="deny",
                reason="file write requires an explicitly approved actor context",
            )
        )

    score = sum(100 if rule.outcome == "deny" else 25 if rule.outcome == "warn" else 0 for rule in rules)
    return PolicyVerdict(score=score, rules=rules)


def evaluate_spreadsheet_write_policy(
        payload: dict[str, Any],
        *,
        allowed_dirs: list[str],
        source_path_keys: list[str],
        actor: str | None = None,
) -> PolicyVerdict:
    rules: list[PolicyRuleResult] = []
    excel_path = _resolve_absolute_path(str(payload.get("excel_file_path") or ""))
    if excel_path is None:
        rules.append(
            PolicyRuleResult(id="spreadsheet-workbook-required", outcome="deny", reason="excel_file_path is required"))
    else:
        rules.append(PolicyRuleResult(id="spreadsheet-workbook-required", outcome="ok"))
    rules.extend(_allowed_path(excel_path, allowed_dirs, rule_id="spreadsheet-workbook-allowlist"))

    for key in source_path_keys:
        source_path = _resolve_absolute_path(str(payload.get(key) or ""))
        if source_path is None:
            rules.append(
                PolicyRuleResult(id=f"spreadsheet-source-required:{key}", outcome="deny", reason=f"{key} is required"))
        else:
            rules.append(PolicyRuleResult(id=f"spreadsheet-source-required:{key}", outcome="ok"))
        rules.extend(_allowed_path(source_path, allowed_dirs, rule_id=f"spreadsheet-source-allowlist:{key}"))

    if actor and actor.startswith("approved/"):
        rules.append(
            PolicyRuleResult(id="spreadsheet-write-approval-context", outcome="ok", reason="actor is approved"))
    else:
        rules.append(
            PolicyRuleResult(
                id="spreadsheet-write-approval-context",
                outcome="deny",
                reason="spreadsheet write requires an explicitly approved actor context",
            )
        )

    score = sum(100 if rule.outcome == "deny" else 25 if rule.outcome == "warn" else 0 for rule in rules)
    return PolicyVerdict(score=score, rules=rules)


def evaluate_markdown_to_word_policy(payload: dict[str, Any], *, actor: str | None = None) -> PolicyVerdict:
    rules: list[PolicyRuleResult] = []
    filename = str(payload.get("filename") or "")
    img_directory = str(payload.get("img_directory") or "")

    if not filename:
        rules.append(PolicyRuleResult(id="document-filename-required", outcome="deny", reason="filename is required"))
    elif "/" in filename or "\\" in filename or path_blocked(filename, include_edit_only=True):
        rules.append(
            PolicyRuleResult(
                id="document-filename-safe",
                outcome="deny",
                reason=f"filename must be a safe document name, not a path: {filename}",
            )
        )
    elif not filename.lower().endswith(".docx"):
        rules.append(
            PolicyRuleResult(
                id="document-filename-extension",
                outcome="warn",
                reason="filename should use the .docx extension",
            )
        )
    else:
        rules.append(PolicyRuleResult(id="document-filename-safe", outcome="ok"))

    if not img_directory:
        rules.append(
            PolicyRuleResult(id="document-artifact-directory-required", outcome="deny",
                             reason="img_directory is required")
        )
    elif ".." in img_directory.replace("\\", "/").split("/"):
        rules.append(
            PolicyRuleResult(
                id="document-artifact-directory-safe",
                outcome="deny",
                reason="img_directory must not contain path traversal",
            )
        )
    else:
        rules.append(PolicyRuleResult(id="document-artifact-directory-safe", outcome="ok"))

    markdown_text = str(payload.get("markdown_text") or "")
    if any(pattern.search(markdown_text) for pattern in DENY_CONTENT_PATTERNS):
        rules.append(
            PolicyRuleResult(
                id="document-no-secrets",
                outcome="deny",
                reason="markdown_text contains a high-risk secret-like pattern",
            )
        )
    else:
        rules.append(PolicyRuleResult(id="document-no-secrets", outcome="ok"))

    if actor and actor.startswith("approved/"):
        rules.append(PolicyRuleResult(id="document-write-approval-context", outcome="ok", reason="actor is approved"))
    else:
        rules.append(
            PolicyRuleResult(
                id="document-write-approval-context",
                outcome="deny",
                reason="document generation requires an explicitly approved actor context",
            )
        )

    score = sum(100 if rule.outcome == "deny" else 25 if rule.outcome == "warn" else 0 for rule in rules)
    return PolicyVerdict(score=score, rules=rules)


def _resolve_target_path(base_folder: str, filename: str) -> Path | None:
    if not base_folder or not filename:
        return None
    try:
        return (Path(base_folder).expanduser() / filename).resolve()
    except Exception:
        return None


def _allowed_directory(target_path: Path | None, allowed_dirs: list[str]) -> list[PolicyRuleResult]:
    return _allowed_path(target_path, allowed_dirs, rule_id="file-write-allowlist")


def _allowed_path(target_path: Path | None, allowed_dirs: list[str], *, rule_id: str) -> list[PolicyRuleResult]:
    if target_path is None:
        return [PolicyRuleResult(id=rule_id, outcome="deny", reason="target path is invalid")]
    allowed_roots = [Path(item).expanduser().resolve() for item in allowed_dirs]
    for root in allowed_roots:
        if target_path == root or root in target_path.parents:
            return [PolicyRuleResult(id=rule_id, outcome="ok", reason="target path is allowlisted")]
    return [
        PolicyRuleResult(
            id=rule_id,
            outcome="deny",
            reason=f"target path is not under an allowlisted directory: {target_path}",
        )
    ]


def _resolve_absolute_path(path: str) -> Path | None:
    if not path:
        return None
    try:
        return Path(path).expanduser().resolve()
    except Exception:
        return None


def _dangerous_file_path(filename: str) -> list[PolicyRuleResult]:
    if filename and path_blocked(filename, include_edit_only=True):
        return [PolicyRuleResult(id="file-write-safe-path", outcome="deny", reason=f"blocked path: {filename}")]
    return [PolicyRuleResult(id="file-write-safe-path", outcome="ok")]


def _likely_secret_content(payload: dict[str, Any]) -> list[PolicyRuleResult]:
    content = str(payload.get("content") or "")
    if any(pattern.search(content) for pattern in DENY_CONTENT_PATTERNS):
        return [
            PolicyRuleResult(
                id="file-write-no-secrets",
                outcome="deny",
                reason="content contains a high-risk secret-like pattern",
            )
        ]
    return [PolicyRuleResult(id="file-write-no-secrets", outcome="ok")]
