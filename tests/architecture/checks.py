from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPO_ROOT / "app"
CREWAI_APP_ROOT = APP_ROOT / "runtime" / "adapters" / "crewai"
SCAN_ROOTS = [
    REPO_ROOT / "app",
    REPO_ROOT / "app.py",
    REPO_ROOT / "tests",
]
DISALLOWED_ROOTS = {"models", "routers", "tools_directory", "util", "utils"}
DISALLOWED_LEGACY_APP_ROOTS = {"models", "routers", "tools_directory", "database"}


def iter_python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
            continue
        if root.is_dir():
            files.extend(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(set(files))


def iter_app_python_files() -> list[Path]:
    return sorted(path for path in APP_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def imported_roots(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name.split(".", 1)[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module.split(".", 1)[0]]
    return []


def import_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module]
    return []


def find_legacy_import_violations() -> list[tuple[Path, int, str]]:
    violations: list[tuple[Path, int, str]] = []
    for path in iter_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for root in imported_roots(node):
                if root in DISALLOWED_ROOTS:
                    violations.append((path.relative_to(REPO_ROOT), node.lineno, root))
    return violations


def find_direct_crewai_import_violations() -> list[tuple[Path, int, str]]:
    violations: list[tuple[Path, int, str]] = []
    for path in iter_app_python_files():
        if path.is_relative_to(CREWAI_APP_ROOT):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for name in import_names(node):
                if name == "crewai" or name.startswith("crewai.") or name == "crewai_tools" or name.startswith(
                        "crewai_tools."):
                    violations.append((path.relative_to(REPO_ROOT), node.lineno, name))
    return violations


def find_legacy_tool_import_violations() -> list[tuple[Path, int, str]]:
    violations: list[tuple[Path, int, str]] = []
    for path in iter_app_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            for name in import_names(node):
                root = name.split(".", 1)[0]
                if root in DISALLOWED_LEGACY_APP_ROOTS:
                    violations.append((path.relative_to(REPO_ROOT), node.lineno, name))
    return violations
