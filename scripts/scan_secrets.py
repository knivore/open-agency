"""Scan a repository for high-signal secrets before they are committed or shared."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple


MAX_FILE_BYTES = 1_000_000
ALLOW_MARKER = "secret-scan: allow"
IGNORED_DIR_NAMES = {
    ".git",
    ".next",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "test-results",
    "venv",
}
LOCAL_ENV_GLOBS = (".env", ".env.*")
PLACEHOLDER_VALUE_RE = re.compile(
    r"(?i)^(?:change-me|example|example-value|fake|dummy|test|sample|replace(?:-me)?|your-.+|none|null|localhost|127\.0\.0\.1)$"
)


class SecretPattern(NamedTuple):
    name: str
    pattern: re.Pattern[str]
    env_only: bool = False


class SecretFinding(NamedTuple):
    path: Path
    line_number: int
    rule: str
    snippet: str


SECRET_PATTERNS: tuple[SecretPattern, ...] = (
    SecretPattern("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    SecretPattern("github-pat", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    SecretPattern("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    SecretPattern("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    SecretPattern("google-api-key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    SecretPattern("slack-token", re.compile(r"\bxox(?:a|b|p|r|s)-[0-9A-Za-z-]{10,}\b")),
    SecretPattern("stripe-live-key", re.compile(r"\bsk_live_[0-9A-Za-z]{16,}\b")),
    SecretPattern(
        "suspicious-env-assignment",
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API[_-]?KEY)[A-Z0-9_]*)\s*=\s*([^\s#]+)"
        ),
        env_only=True,
    ),
)


def _is_binary(data: bytes) -> bool:
    return b"\0" in data


def _looks_like_env_file(path: Path) -> bool:
    name = path.name.lower()
    return name == ".env" or name.startswith(".env.")


def _is_placeholder_value(value: str) -> bool:
    cleaned = value.strip().strip("\"'")
    if not cleaned:
        return True
    if cleaned.lower().startswith("replace-me"):
        return True
    return bool(PLACEHOLDER_VALUE_RE.match(cleaned))


def _git_candidate_paths(repo_root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=repo_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError:
        return []
    return [repo_root / raw_path for raw_path in result.stdout.decode("utf-8").split("\0") if raw_path]


def _local_secret_paths(repo_root: Path) -> list[Path]:
    paths: set[Path] = set()
    # Include ignored local env files so developers catch leaks before staging them.
    for glob in LOCAL_ENV_GLOBS:
        paths.update(path for path in repo_root.glob(glob) if path.is_file())
    return sorted(paths)


def iter_candidate_paths(repo_root: Path) -> list[Path]:
    resolved_repo_root = repo_root.resolve()
    candidates = {path.resolve() for path in _git_candidate_paths(repo_root)}
    candidates.update(path.resolve() for path in _local_secret_paths(repo_root))
    filtered: list[Path] = []
    for path in sorted(candidates):
        try:
            relative_parts = path.relative_to(resolved_repo_root).parts
        except ValueError:
            continue
        if any(part in IGNORED_DIR_NAMES for part in relative_parts[:-1]):
            continue
        if not path.is_file():
            continue
        filtered.append(path)
    return filtered


def scan_text(path: Path, text: str) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    is_env_file = _looks_like_env_file(path)
    for line_number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for secret_pattern in SECRET_PATTERNS:
            if secret_pattern.env_only and not is_env_file:
                continue
            match = secret_pattern.pattern.search(line)
            if not match:
                continue
            if secret_pattern.name == "suspicious-env-assignment" and _is_placeholder_value(match.group(2)):
                continue
            findings.append(
                SecretFinding(
                    path=path,
                    line_number=line_number,
                    rule=secret_pattern.name,
                    snippet=line.strip(),
                )
            )
            break
    return findings


def scan_repo(repo_root: Path, *, max_file_bytes: int = MAX_FILE_BYTES) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    resolved_repo_root = repo_root.resolve()
    for path in iter_candidate_paths(repo_root):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) > max_file_bytes or _is_binary(data):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(path.relative_to(resolved_repo_root), text))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="Repository root to scan.")
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=MAX_FILE_BYTES,
        help="Skip files larger than this many bytes.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    findings = scan_repo(repo_root, max_file_bytes=args.max_file_bytes)
    if not findings:
        print(f"secret-scan: no high-signal secrets found in {repo_root}")
        return 0

    print(f"secret-scan: found {len(findings)} potential secret(s) in {repo_root}", file=sys.stderr)
    for finding in findings:
        print(
            f"{finding.path}:{finding.line_number}: [{finding.rule}] {finding.snippet}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
