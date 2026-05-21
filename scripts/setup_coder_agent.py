#!/usr/bin/env python3
"""Compatibility entrypoint for Coder agent setup."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _bootstrap_repo(script_file: str, *, reexec: bool) -> Path:
    script_path = Path(script_file).resolve()
    repo_root = script_path.parents[1]
    if reexec:
        venv_dir = repo_root / ".venv"
        venv_python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if venv_python.exists() and Path(sys.prefix).resolve() != venv_dir.resolve():
            os.execv(str(venv_python), [str(venv_python), str(script_path), *sys.argv[1:]])
    repo_root_text = str(repo_root)
    if repo_root_text not in sys.path:
        sys.path.insert(0, repo_root_text)
    return repo_root


_bootstrap_repo(__file__, reexec=__name__ == "__main__")

from scripts.setup import main, setup_coder_agent

__all__ = ["setup_coder_agent"]


if __name__ == "__main__":
    raise SystemExit(main(["coder-agent", *sys.argv[1:]]))
