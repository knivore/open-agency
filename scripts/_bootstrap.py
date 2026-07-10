#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


def bootstrap_repo(script_file: str, *, reexec: bool) -> Path:
    script_path = Path(script_file).resolve()
    repo_root = script_path.parents[1]
    if reexec:
        _reexec_from_repo_venv(script_path, repo_root)
    repo_root_text = str(repo_root)
    if repo_root_text not in sys.path:
        sys.path.insert(0, repo_root_text)
    return repo_root


def _reexec_from_repo_venv(script_path: Path, repo_root: Path) -> None:
    venv_dir = repo_root / ".venv"
    venv_python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not venv_python.exists():
        return
    if Path(sys.prefix).resolve() == venv_dir.resolve():
        return
    os.execv(str(venv_python), [str(venv_python), str(script_path), *sys.argv[1:]])
