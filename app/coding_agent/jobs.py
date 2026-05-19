from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .workspaces import BACKEND_WORKSPACE, resolve_workspace

JobStatus = Literal["queued", "running", "completed", "failed", "needs_review"]

DEFAULT_JOB_ROOT = BACKEND_WORKSPACE / "var" / "coding_jobs"
DEFAULT_SUGGESTED_COMMANDS = ["git status", "npm test", "npm run build", "pytest"]


@dataclass(frozen=True)
class CodingJob:
    id: str
    title: str
    description: str
    workspace: str
    requested_by: str | None
    status: JobStatus
    task_md_path: str
    codex_stdout_path: str
    codex_stderr_path: str
    git_status_path: str
    git_diff_path: str
    test_output_path: str
    summary_md_path: str
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None


def create_coding_job(
    *,
    title: str,
    description: str,
    workspace: str | Path,
    requested_by: str | None = None,
    original_request: str | None = None,
    job_root: str | Path = DEFAULT_JOB_ROOT,
    job_id: str | None = None,
    suggested_commands: list[str] | None = None,
) -> CodingJob:
    resolved_workspace = resolve_workspace(workspace)
    normalized_title = title.strip()
    normalized_description = description.strip()
    if not normalized_title:
        raise ValueError("Coding job title is required.")
    if not normalized_description:
        raise ValueError("Coding job description is required.")

    effective_job_id = _safe_job_id(job_id)
    job_dir = Path(job_root).expanduser().resolve() / effective_job_id
    job_dir.mkdir(parents=True, exist_ok=False)

    task_md_path = job_dir / "task.md"
    job = CodingJob(
        id=effective_job_id,
        title=normalized_title,
        description=normalized_description,
        workspace=str(resolved_workspace),
        requested_by=requested_by,
        status="queued",
        task_md_path=str(task_md_path),
        codex_stdout_path=str(job_dir / "codex_stdout.log"),
        codex_stderr_path=str(job_dir / "codex_stderr.log"),
        git_status_path=str(job_dir / "git_status.txt"),
        git_diff_path=str(job_dir / "git_diff.patch"),
        test_output_path=str(job_dir / "test_output.log"),
        summary_md_path=str(job_dir / "summary.md"),
        created_at=_utc_now(),
    )
    task_md_path.write_text(
        render_task_markdown(
            title=normalized_title,
            description=normalized_description,
            workspace=str(resolved_workspace),
            original_request=original_request or normalized_description,
            suggested_commands=suggested_commands or DEFAULT_SUGGESTED_COMMANDS,
        ),
        encoding="utf-8",
    )
    (job_dir / "job.json").write_text(json.dumps(asdict(job), indent=2, sort_keys=True), encoding="utf-8")
    return job


def render_task_markdown(
    *,
    title: str,
    description: str,
    workspace: str,
    original_request: str,
    suggested_commands: list[str],
) -> str:
    commands = "\n".join(suggested_commands)
    return f"""# Coding Task

## Objective
{title}

## Workspace
{workspace}

## Description
{description}

## Constraints
- Stay within the selected workspace.
- Do not access secrets or credential files.
- Do not delete files or folders unless deletion is explicitly approved.
- Do not push to remote git.
- Prefer small, focused changes.
- Update or add tests where appropriate.
- Do not perform destructive commands.

## Expected Deliverables
- Code changes implemented.
- Relevant tests/build checks run.
- Summary of files changed.
- Any unresolved issues clearly stated.

## Suggested Commands
```bash
{commands}
```

## User Request
{original_request}
"""


def _safe_job_id(job_id: str | None) -> str:
    candidate = (job_id or str(uuid4())).strip()
    if not candidate:
        raise ValueError("Coding job id cannot be empty.")
    if any(part == ".." for part in Path(candidate).parts):
        raise ValueError("Coding job id cannot contain path traversal.")
    if any(char in candidate for char in "/\\"):
        raise ValueError("Coding job id must be a single path segment.")
    return candidate


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
