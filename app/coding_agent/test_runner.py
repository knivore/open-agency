from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .workspaces import resolve_workspace


def run_command(workspace: str | Path, command: list[str], timeout_seconds: int = 600) -> str:
    if not command:
        raise ValueError("Command is required.")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero.")

    repo = resolve_workspace(workspace)
    result = subprocess.run(
        command,
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    output = "\n".join(part for part in [result.stdout.rstrip(), result.stderr.rstrip()] if part)
    if result.returncode != 0:
        output = f"{output}\nCommand failed with exit code {result.returncode}".strip()
    return output


def run_default_checks(workspace: str | Path) -> str:
    repo = resolve_workspace(workspace)
    outputs: list[str] = []

    package_json = repo / "package.json"
    if package_json.exists():
        scripts = _read_package_scripts(package_json)
        if "build" in scripts:
            outputs.append("## npm run build")
            outputs.append(run_command(repo, ["npm", "run", "build"], 900))
        if "test" in scripts:
            outputs.append("## npm test")
            outputs.append(run_command(repo, ["npm", "test"], 900))

    has_python_tests = any((repo / marker).exists() for marker in ("pyproject.toml", "pytest.ini", "tests"))
    if has_python_tests:
        python = _python_executable(repo)
        pytest_available = _module_available(repo, python, "pytest")
        if pytest_available:
            outputs.append("## python -m pytest")
            outputs.append(run_command(repo, [python, "-m", "pytest"], 900))
        else:
            outputs.append("## python -m unittest discover")
            outputs.append(run_command(repo, [python, "-m", "unittest", "discover"], 900))

    if not outputs:
        outputs.append("No default test command detected.")

    return "\n\n".join(outputs)


def _read_package_scripts(package_json: Path) -> dict[str, str]:
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = payload.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


def _python_executable(repo: Path) -> str:
    venv_python = repo / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return shutil.which("python3") or "python"


def _module_available(repo: Path, python: str, module_name: str) -> bool:
    result = subprocess.run(
        [python, "-c", f"import {module_name}"],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0
