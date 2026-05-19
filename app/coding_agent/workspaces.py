from __future__ import annotations

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_WORKSPACE = Path(os.getenv("AGENCY_BACKEND_WORKSPACE", str(REPO_ROOT))).expanduser().resolve()
FRONTEND_WORKSPACE = Path(
    os.getenv("AGENCY_FRONTEND_WORKSPACE", str(REPO_ROOT.parent / "agency-fe"))
).expanduser().resolve()

ALLOWED_WORKSPACES: dict[str, Path] = {
    "agency": BACKEND_WORKSPACE,
    "agency-backend": BACKEND_WORKSPACE,
    "backend": BACKEND_WORKSPACE,
    "agency-fe": FRONTEND_WORKSPACE,
    "agency-frontend": FRONTEND_WORKSPACE,
    "frontend": FRONTEND_WORKSPACE,
}
_BLOCKED_PATH_PARTS = frozenset(
    {
        ".aws",
        ".codex",
        ".config",
        ".docker",
        ".gnupg",
        ".kube",
        ".ssh",
    }
)
_BLOCKED_FILE_NAMES = frozenset({".env"})


class WorkspaceResolutionError(ValueError):
    pass


def resolve_workspace(input_path: str | Path) -> Path:
    raw = str(input_path).strip()
    if not raw:
        raise WorkspaceResolutionError("Workspace is required.")

    alias = raw.lower()
    if alias in ALLOWED_WORKSPACES:
        return _require_existing_workspace(ALLOWED_WORKSPACES[alias])

    if _contains_parent_traversal(raw):
        raise WorkspaceResolutionError(f"Workspace path traversal is not allowed: {raw}")

    requested = Path(raw).expanduser().resolve()
    return _require_existing_workspace(requested)


def resolve_task_file(task_md_path: str | Path) -> Path:
    raw = str(task_md_path).strip()
    if not raw:
        raise WorkspaceResolutionError("Task markdown path is required.")
    if _contains_parent_traversal(raw):
        raise WorkspaceResolutionError(f"Task path traversal is not allowed: {raw}")

    task_file = Path(raw).expanduser().resolve()
    if _is_blocked_path(task_file):
        raise WorkspaceResolutionError(f"Task file path is blocked because it may expose credentials: {task_file}")
    if task_file.suffix.lower() != ".md":
        raise WorkspaceResolutionError(f"Task file must be markdown: {task_file}")
    if not task_file.exists():
        raise FileNotFoundError(f"Task file not found: {task_file}")
    if not task_file.is_file():
        raise WorkspaceResolutionError(f"Task path is not a file: {task_file}")
    return task_file


def _require_existing_workspace(path: Path) -> Path:
    if _is_blocked_path(path):
        raise WorkspaceResolutionError(f"Workspace path is blocked because it may expose credentials: {path}")
    if not path.exists() or not path.is_dir():
        raise WorkspaceResolutionError(f"Workspace does not exist or is not a directory: {path}")
    return path


def _contains_parent_traversal(raw: str) -> bool:
    return any(part == ".." for part in Path(raw).parts)


def _is_blocked_path(path: Path) -> bool:
    parts = set(path.parts)
    return bool(parts & _BLOCKED_PATH_PARTS) or path.name in _BLOCKED_FILE_NAMES
