from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .workspaces import resolve_task_file, resolve_workspace


@dataclass(frozen=True)
class CodexRunResult:
    returncode: int
    stdout: str
    stderr: str
    command: list[str]
    workspace: str
    task_md_path: str
    duration_ms: int
    timed_out: bool = False


def run_codex_job(
    workspace: str | Path,
    task_md_path: str | Path,
    timeout_seconds: int = 1800,
    *,
    codex_binary: str = "codex",
    sandbox: str = "workspace-write",
) -> CodexRunResult:
    repo = resolve_workspace(workspace)
    task_file = resolve_task_file(task_md_path)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero.")
    if sandbox not in {"read-only", "workspace-write"}:
        raise ValueError("sandbox must be 'read-only' or 'workspace-write'.")

    executable = shutil.which(codex_binary)
    if executable is None:
        raise FileNotFoundError(f"Codex CLI not found on PATH: {codex_binary}")

    prompt = _build_codex_prompt(task_file)
    command = [
        executable,
        "exec",
        "--sandbox",
        sandbox,
        prompt,
    ]

    started_at = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=repo,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        return CodexRunResult(
            returncode=124,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + f"\nCodex job timed out after {timeout_seconds}s.",
            command=command,
            workspace=str(repo),
            task_md_path=str(task_file),
            duration_ms=duration_ms,
            timed_out=True,
        )

    return CodexRunResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        command=command,
        workspace=str(repo),
        task_md_path=str(task_file),
        duration_ms=int((time.perf_counter() - started_at) * 1000),
    )


def _build_codex_prompt(task_file: Path) -> str:
    return f"""Read the coding task below and implement it in this repository.

Task file: {task_file}

Rules:
- Stay inside this workspace.
- Do not push to remote git.
- Do not access secrets or credential files.
- Do not delete files or folders unless the task explicitly includes deletion approval.
- Run relevant tests or build checks where practical.
- Do not perform destructive commands.
- End by summarizing changed files, tests run, and unresolved issues.
"""
