from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path


class GitSandboxError(RuntimeError):
    pass


def ensure_allowed_git_repo(repo: str, allowed_repos: list[str]) -> Path:
    resolved = Path(repo).expanduser().resolve()
    allowed = {Path(item).expanduser().resolve() for item in allowed_repos}
    if resolved not in allowed:
        raise GitSandboxError(f"Repository is not allowlisted: {repo}")
    if not (resolved / ".git").exists():
        raise GitSandboxError(f"Repository is not a git repository: {resolved}")
    return resolved


def build_sandbox_branch_name(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    return f"agent/{current.strftime('%Y%m%d%H%M%S')}-sandbox"


def get_repo_diff(repo: Path) -> str:
    return _run_git(repo, ["diff", "--"])


def _run_git(repo: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise GitSandboxError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout
