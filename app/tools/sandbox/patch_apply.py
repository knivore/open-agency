from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from app.tools.contracts.models import FileChanged


class PatchApplyError(RuntimeError):
    pass


def apply_patch_dry_run(repo: Path, changes: list[dict[str, Any]]) -> str:
    patch = build_combined_patch(changes)
    if not patch.strip():
        return ""
    result = subprocess.run(
        ["git", "apply", "--check", "--"],
        cwd=repo,
        input=patch,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise PatchApplyError(result.stderr.strip() or "patch dry-run failed")
    return patch


def build_combined_patch(changes: list[dict[str, Any]]) -> str:
    patch = "\n".join(str(change.get("patch") or "").rstrip() for change in changes if change.get("patch"))
    return f"{patch}\n" if patch else ""


def summarize_files_changed(changes: list[dict[str, Any]]) -> list[FileChanged]:
    return [FileChanged(path=str(change.get("path")), op=_infer_op(str(change.get("patch") or ""))) for change in
            changes]


def _infer_op(patch: str) -> str:
    if "\nnew file mode " in patch:
        return "create"
    if "\ndeleted file mode " in patch:
        return "delete"
    if "\nrename from " in patch and "\nrename to " in patch:
        return "rename"
    return "modify"
