from __future__ import annotations

import subprocess
from pathlib import Path

from .workspaces import resolve_workspace


class GitCommandError(RuntimeError):
    pass


def run_git(workspace: str | Path, args: list[str], *, timeout_seconds: int = 120) -> str:
    repo = resolve_workspace(workspace)
    _validate_git_args(args)
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        raise GitCommandError(result.stderr.strip() or f"git {' '.join(args)} failed with {result.returncode}")
    return result.stdout


def get_git_status(workspace: str | Path) -> str:
    return run_git(workspace, ["status", "--short"])


def get_git_diff(workspace: str | Path) -> str:
    return run_git(workspace, ["diff", "--"])


def _validate_git_args(args: list[str]) -> None:
    if not args:
        raise GitCommandError("Git arguments are required.")
    if args[0] in {"push", "reset", "checkout", "clean"}:
        raise GitCommandError(f"Blocked git command for coding jobs: git {args[0]}")
