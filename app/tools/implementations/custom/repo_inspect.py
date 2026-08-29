from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from pydantic import BaseModel, Field
from typing import Any

from app.core.config import get_settings
from app.runtime.workspace_paths import default_repo_write_mounts


class RepoInspectInput(BaseModel):
    repo: str = Field(..., description="Repository alias or allowlisted absolute path.")
    query: str | None = Field(default=None, description="Optional case-insensitive text to search for in files.")
    focus_paths: list[str] = Field(
        default_factory=list,
        description="Optional repo-relative files or glob patterns to prioritize in the result.",
    )
    include_patterns: list[str] = Field(
        default_factory=list,
        description="Optional repo-relative glob patterns to include in addition to the default scan set.",
    )
    exclude_patterns: list[str] = Field(
        default_factory=list,
        description="Optional repo-relative glob patterns to exclude from the scan.",
    )
    max_files: int = Field(default=24, ge=1, le=200, description="Maximum number of files to scan for matches.")
    max_hits: int = Field(default=40, ge=1, le=200, description="Maximum number of TODO or query hits to return.")
    excerpt_line_limit: int = Field(
        default=24,
        ge=4,
        le=120,
        description="Maximum number of lines to include in each file excerpt.",
    )


DEFAULT_EXCLUDE_PATTERNS = {
    ".git/*",
    ".next/*",
    ".venv/*",
    "__pycache__/*",
    "build/*",
    "coverage/*",
    "dist/*",
    "node_modules/*",
    "out/*",
    "tmp/*",
}
DEFAULT_PRIORITY_FILES = (
    "README.md",
    "docs/*.md",
    "app/**/*.py",
    "src/**/*",
    "package.json",
    "pnpm-lock.yaml",
    "package-lock.json",
    "pyproject.toml",
    "requirements*.txt",
)
TEXT_FILE_SUFFIXES = {
    ".c",
    ".cc",
    ".cfg",
    ".css",
    ".env",
    ".example",
    ".go",
    ".h",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
TODO_MARKERS = ("TODO", "FIXME", "HACK", "XXX", "BUG")
STATUS_PREVIEW_LIMIT = 80


def inspect_repo(
        repo: str,
        query: str | None = None,
        focus_paths: list[str] | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        max_files: int = 24,
        max_hits: int = 40,
        excerpt_line_limit: int = 24,
        **_: Any,
) -> dict[str, Any]:
    payload = RepoInspectInput(
        repo=repo,
        query=query,
        focus_paths=focus_paths or [],
        include_patterns=include_patterns or [],
        exclude_patterns=exclude_patterns or [],
        max_files=max_files,
        max_hits=max_hits,
        excerpt_line_limit=excerpt_line_limit,
    )
    repo_id, repo_path = _resolve_repo(payload.repo)
    tracked_files = _git_lines(repo_path, "ls-files")
    untracked_files = _git_lines(repo_path, "ls-files", "--others", "--exclude-standard")
    all_files = _filter_repo_files(
        repo_path=repo_path,
        files=[*tracked_files, *untracked_files],
        include_patterns=payload.include_patterns,
        exclude_patterns=payload.exclude_patterns,
    )
    prioritized = _prioritize_files(
        files=all_files,
        focus_paths=payload.focus_paths,
        query=payload.query,
    )
    scan_files = prioritized[: payload.max_files]
    # Keep the returned file list bounded for readability, but search the full
    # prioritized set so daily review workflows do not miss late-sorted hits.
    todo_hits, query_hits = _collect_hits(
        repo_path=repo_path,
        files=prioritized,
        query=payload.query,
        max_hits=payload.max_hits,
    )
    excerpt_paths = _excerpt_paths(scan_files, todo_hits, query_hits)
    file_excerpts = [
        _read_excerpt(repo_path / relative_path, repo_path=repo_path, line_limit=payload.excerpt_line_limit)
        for relative_path in excerpt_paths
    ]

    status_short = _git_lines(repo_path, "status", "--short", "--branch")
    return {
        "status": "ok",
        "repo_id": repo_id,
        "repo_path": str(repo_path),
        "branch": _git_stdout(repo_path, "branch", "--show-current"),
        "head_commit": _git_stdout(repo_path, "rev-parse", "HEAD"),
        # A dirty checkout can contain hundreds of generated or unrelated changes.
        # Keep the inspection result useful to the model without allowing git status
        # to crowd the actual source excerpts out of the context window.
        "status_short": status_short[:STATUS_PREVIEW_LIMIT],
        "status_short_total": len(status_short),
        "status_short_truncated": len(status_short) > STATUS_PREVIEW_LIMIT,
        "recent_commits": _recent_commits(repo_path),
        "tracked_file_count": len(tracked_files),
        "untracked_file_count": len(untracked_files),
        "scanned_files": scan_files,
        "todo_hits": todo_hits,
        "query_hits": query_hits,
        "file_excerpts": file_excerpts,
    }


def _resolve_repo(repo: str) -> tuple[str, Path]:
    candidate = repo.strip()
    if not candidate:
        raise ValueError("repo is required")

    # The coaching workflow must be able to inspect the canonical repos on a schedule
    # without going through the approval-gated shell tool. Resolve only explicit
    # allowlisted repos, plus local development mirrors for the standard repo aliases.
    allowed_candidates = _allowed_repo_candidates()
    resolved_candidates: dict[str, Path] = {}
    for item in allowed_candidates:
        resolved_candidates[str(item)] = item
        # Reserve basename aliases for the first matching allowlisted repo so an
        # explicit temp repo named "agency" is not shadowed by local fallbacks.
        resolved_candidates.setdefault(item.name, item)

    exact_path = Path(candidate).expanduser()
    if exact_path.is_absolute():
        resolved = exact_path.resolve()
        for allowed in allowed_candidates:
            if resolved == allowed.resolve():
                # Preserve the caller-facing path spelling on macOS, where /var resolves through /private/var.
                return allowed.name, exact_path
        raise ValueError(f"Repository is not allowlisted: {repo}")

    resolved = resolved_candidates.get(candidate)
    if resolved is None:
        raise ValueError(
            f"Unknown repo '{repo}'. Use one of: {', '.join(sorted({item.name for item in allowed_candidates}))}"
        )
    return resolved.name, resolved


def _allowed_repo_candidates() -> list[Path]:
    settings = get_settings()
    configured_candidates = [
        Path(item).expanduser().absolute()
        for item in settings.parsed_sandbox_edit_allowed_repos
        if item.strip()
    ]
    candidates = [
        item for item in configured_candidates if item.exists() and (item / ".git").exists()
    ]
    repo_root = Path(__file__).resolve().parents[4]
    # Local mirrors are a convenience for developer environments that do not have
    # a usable allowlist configured. Once an explicit repo exists, do not widen
    # inspection scope with extra fallback aliases.
    if not candidates:
        # Containerized backends run from /app, which is not the repository root.
        # Include the canonical runtime mount targets before source-tree fallbacks.
        runtime_targets = [Path(mount["target"]).expanduser() for mount in default_repo_write_mounts()]
        for fallback in (*runtime_targets, repo_root, repo_root.parent / "open-agency-fe"):
            if fallback.exists() and (fallback / ".git").exists():
                candidates.append(fallback.resolve())
    unique: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _git_stdout(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip() or f"git {' '.join(args)} failed"
        raise ValueError(error)
    return result.stdout.strip()


def _git_lines(repo_path: Path, *args: str) -> list[str]:
    output = _git_stdout(repo_path, *args)
    if not output:
        return []
    return [line for line in output.splitlines() if line.strip()]


def _recent_commits(repo_path: Path, *, limit: int = 5) -> list[dict[str, str]]:
    raw = _git_lines(repo_path, "log", f"-n{limit}", "--date=iso-strict", "--pretty=format:%H%x1f%ad%x1f%s")
    commits: list[dict[str, str]] = []
    for line in raw:
        sha, authored_at, subject = (line.split("\x1f", 2) + ["", "", ""])[:3]
        commits.append(
            {
                "sha": sha,
                "short_sha": sha[:10],
                "authored_at": authored_at,
                "subject": subject,
            }
        )
    return commits


def _filter_repo_files(
        *,
        repo_path: Path,
        files: list[str],
        include_patterns: list[str],
        exclude_patterns: list[str],
) -> list[str]:
    normalized_excludes = {pattern.strip() for pattern in [*DEFAULT_EXCLUDE_PATTERNS, *exclude_patterns] if
                           pattern.strip()}
    include_globs = [pattern.strip() for pattern in include_patterns if pattern.strip()]
    filtered: list[str] = []
    for relative_path in files:
        normalized = relative_path.replace("\\", "/")
        if any(fnmatch.fnmatch(normalized, pattern) for pattern in normalized_excludes):
            continue
        file_path = repo_path / normalized
        if not file_path.is_file() or not _is_text_file(file_path):
            continue
        if include_globs and not any(fnmatch.fnmatch(normalized, pattern) for pattern in include_globs):
            continue
        filtered.append(normalized)
    return filtered


def _prioritize_files(files: list[str], *, focus_paths: list[str], query: str | None) -> list[str]:
    query_lower = (query or "").strip().lower()
    focus_patterns = [pattern.strip().replace("\\", "/") for pattern in focus_paths if pattern.strip()]

    def score(path: str) -> tuple[int, str]:
        value = 0
        if any(path == pattern or fnmatch.fnmatch(path, pattern) for pattern in focus_patterns):
            value += 100
        if any(fnmatch.fnmatch(path, pattern) for pattern in DEFAULT_PRIORITY_FILES):
            value += 25
        if query_lower and query_lower in path.lower():
            value += 20
        if "/docs/" in f"/{path}":
            value += 10
        if path.endswith(("README.md", "package.json", "pyproject.toml")):
            value += 10
        return -value, path

    return sorted(files, key=score)


def _collect_hits(
        *,
        repo_path: Path,
        files: list[str],
        query: str | None,
        max_hits: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    todo_hits: list[dict[str, Any]] = []
    query_hits: list[dict[str, Any]] = []
    query_lower = (query or "").strip().lower()
    for relative_path in files:
        file_path = repo_path / relative_path
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if len(todo_hits) < max_hits and any(marker in line for marker in TODO_MARKERS):
                todo_hits.append({"path": relative_path, "line": index, "text": stripped[:240]})
            if query_lower and len(query_hits) < max_hits and query_lower in line.lower():
                query_hits.append({"path": relative_path, "line": index, "text": stripped[:240]})
            if len(todo_hits) >= max_hits and (not query_lower or len(query_hits) >= max_hits):
                return todo_hits, query_hits
    return todo_hits, query_hits


def _excerpt_paths(
        scan_files: list[str],
        todo_hits: list[dict[str, Any]],
        query_hits: list[dict[str, Any]],
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    # Always return the files the caller explicitly asked to scan first. A focused
    # inspection must contain its target even when unrelated TODOs were found in
    # files elsewhere in the repository.
    for path in scan_files:
        if path in seen:
            continue
        seen.add(path)
        ordered.append(path)
        if len(ordered) >= 6:
            return ordered
    for item in [*query_hits, *todo_hits]:
        path = str(item.get("path") or "")
        if path and path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered[:6]


def _read_excerpt(file_path: Path, *, repo_path: Path, line_limit: int) -> dict[str, Any]:
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    excerpt_lines = lines[:line_limit]
    return {
        "path": str(file_path.relative_to(repo_path)).replace("\\", "/"),
        "line_start": 1,
        "line_end": len(excerpt_lines),
        "excerpt": "\n".join(excerpt_lines),
    }


def _is_text_file(file_path: Path) -> bool:
    if file_path.suffix.lower() in TEXT_FILE_SUFFIXES:
        return True
    return file_path.name.lower() in {"dockerfile", "makefile", ".gitignore", ".dockerignore", ".env.example"}
