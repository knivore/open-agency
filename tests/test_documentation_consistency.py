from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from app.api.context import create_test_api_context
from app.api.routes import create_api_router


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = REPO_ROOT.parent / "open-agency-fe"
EXCLUDED_DOC_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "TODO",
}
SOURCE_REF_PREFIXES = (
    "app/",
    "scripts/",
    "tests/",
    "docker/",
    "alembic/",
    "integrations/",
    "evals/",
    "database_exports/",
    "components/",
    "lib/",
    "modules/",
    "hooks/",
    "auth.ts",
    "proxy.ts",
)
FRONTEND_SOURCE_REF_PREFIXES = (
    "components/",
    "lib/",
    "modules/",
    "hooks/",
    "auth.ts",
    "proxy.ts",
)
PYTHON_SOURCE_REF_PREFIXES = ("app/", "scripts/")
INTENTIONAL_NON_SOURCE_REFS = {
    "app/security/",
    "app/tools/contracts/schemas/<tool>.contract.json",
}
FRONTEND_OR_EXTERNAL_ENDPOINT_PREFIXES = (
    "/api/auth",
    "/api/crew",
    "/api/history",
    "/api/artifacts",
    "/api/hitl",
    "/api/embed",
    "/api/device-events",
    "/api/devices",
    "/api/physical/events",
    "/api/smart-home",
    "/api/xr/simulator",
    "/backend",
    "/login",
    "/auth/",
    "/observatory",
    "/operations",
    "/runs",
    "/runtime",
    "/v1/",
)


def _is_reviewed_doc(path: Path) -> bool:
    return not any(part in EXCLUDED_DOC_PARTS for part in path.relative_to(REPO_ROOT).parts)


def _collect_maintained_docs() -> list[Path]:
    return [
        REPO_ROOT / "README.md",
        *sorted(path for path in (REPO_ROOT / "docs").rglob("*.md") if _is_reviewed_doc(path)),
    ]


def _collect_reviewed_docs() -> list[Path]:
    docs = set(_collect_maintained_docs())
    docs.update(path for path in REPO_ROOT.rglob("README.md") if _is_reviewed_doc(path))
    docs.update(path for path in (REPO_ROOT / "docs" / "architecture" / "diagrams").glob("*.mmd"))
    return sorted(docs)


def _documented_source_candidates(doc: Path) -> list[str]:
    text = doc.read_text(encoding="utf-8")
    candidates = [
        *re.findall(r"`([^`\n]+)`", text),
        *re.findall(r"\[[^\]]+\]\(([^)]+)\)", text),
        *re.findall(r"Source:\s*([^\"\]\n]+)", text),
    ]
    source_refs: list[str] = []
    for raw_candidate in candidates:
        candidate = raw_candidate.split("#", 1)[0].strip()
        if not candidate or "://" in candidate or candidate.startswith(("/", "mailto:", "--")):
            continue
        for token in re.split(r"[, ]+", candidate):
            source_refs.append(token.strip(".,:;)("))
    return source_refs


MAINTAINED_DOCS = _collect_maintained_docs()
REVIEWED_DOCS = _collect_reviewed_docs()
REVIEWED_VISUALS = sorted((REPO_ROOT / "docs" / "architecture" / "visuals").glob("*.svg"))


class DocumentationConsistencyTests(unittest.TestCase):
    def test_reviewed_markdown_links_resolve(self) -> None:
        missing: list[tuple[str, str]] = []
        for doc in [*REVIEWED_DOCS, *REVIEWED_VISUALS]:
            for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", doc.read_text(encoding="utf-8")):
                target = link.split("#", 1)[0]
                if not target or re.match(r"^[a-z]+://", target) or target.startswith("mailto:"):
                    continue
                path = Path(target) if target.startswith("/") else (doc.parent / target).resolve()
                if not path.exists():
                    missing.append((str(doc.relative_to(REPO_ROOT)), link))

        self.assertEqual(missing, [])

    def test_reviewed_docs_do_not_contain_local_absolute_paths(self) -> None:
        local_root_pattern = re.escape(str(REPO_ROOT.parent))
        leaks: list[tuple[str, int, str]] = []
        for doc in [*REVIEWED_DOCS, *REVIEWED_VISUALS]:
            for line_number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1):
                if re.search(local_root_pattern, line):
                    leaks.append((str(doc.relative_to(REPO_ROOT)), line_number, line.strip()))

        self.assertEqual(leaks, [])

    def test_documented_repo_script_paths_exist(self) -> None:
        missing: list[tuple[str, str]] = []
        pattern = re.compile(r"\bscripts/[A-Za-z0-9_./-]+")
        for doc in REVIEWED_DOCS:
            for match in pattern.findall(doc.read_text(encoding="utf-8")):
                script = match.rstrip(".,:;)")
                if not (REPO_ROOT / script).exists():
                    missing.append((str(doc.relative_to(REPO_ROOT)), script))

        self.assertEqual(missing, [])

    def test_documented_source_paths_exist(self) -> None:
        missing: list[tuple[str, str]] = []
        for doc in REVIEWED_DOCS:
            for raw_source_ref in _documented_source_candidates(doc):
                source_ref = self._normalize_source_ref(raw_source_ref)
                if (
                    not source_ref
                    or source_ref in INTENTIONAL_NON_SOURCE_REFS
                    or "<" in source_ref
                    or "(" in source_ref
                    or not source_ref.startswith(SOURCE_REF_PREFIXES)
                ):
                    continue
                roots = self._source_ref_roots(raw_source_ref, source_ref)
                if not roots:
                    continue
                if "*" in source_ref:
                    found = any(list(root.glob(source_ref)) for root in roots)
                else:
                    found = any((root / source_ref).exists() for root in roots)
                if not found:
                    missing.append((str(doc.relative_to(REPO_ROOT)), source_ref))

        self.assertEqual(missing, [])

    def test_documented_backend_python_files_have_module_docstrings(self) -> None:
        missing: list[tuple[str, str]] = []
        inspected: set[str] = set()
        for doc in REVIEWED_DOCS:
            for raw_source_ref in _documented_source_candidates(doc):
                source_ref = self._normalize_source_ref(raw_source_ref)
                if (
                    source_ref in inspected
                    or not source_ref.endswith(".py")
                    or not source_ref.startswith(PYTHON_SOURCE_REF_PREFIXES)
                    or "<" in source_ref
                    or "(" in source_ref
                ):
                    continue
                inspected.add(source_ref)
                path = REPO_ROOT / source_ref
                if not path.exists():
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"))
                if not ast.get_docstring(tree):
                    missing.append((str(doc.relative_to(REPO_ROOT)), source_ref))

        self.assertEqual(missing, [])

    def test_settings_aliases_are_covered_by_maintained_docs(self) -> None:
        tree = ast.parse((REPO_ROOT / "app/core/config.py").read_text(encoding="utf-8"))
        aliases: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "Field":
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "alias"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    aliases.add(keyword.value.value)

        docs_text = "\n".join(doc.read_text(encoding="utf-8") for doc in MAINTAINED_DOCS)
        missing = sorted(alias for alias in aliases if alias not in docs_text)
        self.assertEqual(missing, [])

    def test_documented_backend_http_endpoints_are_registered(self) -> None:
        route_pairs: set[tuple[str, str]] = set()
        for route in create_api_router(create_test_api_context()).routes:
            path = getattr(route, "path", "")
            for method in getattr(route, "methods", set()) or set():
                if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    continue
                route_pairs.add((method, self._normalize_endpoint_path(path)))

        missing: list[tuple[str, str, str]] = []
        endpoint_pattern = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[^\s`),]+)")
        for doc in REVIEWED_DOCS:
            text = doc.read_text(encoding="utf-8")
            for method, raw_path in endpoint_pattern.findall(text):
                path = raw_path.split("?", 1)[0].rstrip(".,:;")
                if self._is_external_or_frontend_endpoint(path):
                    continue
                normalized_path = self._normalize_endpoint_path(path)
                if (method, normalized_path) not in route_pairs:
                    missing.append((str(doc.relative_to(REPO_ROOT)), method, path))

        self.assertEqual(missing, [])

    @staticmethod
    def _normalize_endpoint_path(path: str) -> str:
        return re.sub(r"\{[^}/]+\}", "{}", path)

    @staticmethod
    def _normalize_source_ref(source_ref: str) -> str:
        if source_ref.startswith("open-agency/"):
            return source_ref.removeprefix("open-agency/")
        if source_ref.startswith("open-agency-fe/"):
            return source_ref.removeprefix("open-agency-fe/")
        if source_ref.startswith("agency/"):
            return source_ref.removeprefix("agency/")
        if source_ref.startswith("agency-fe/"):
            return source_ref.removeprefix("agency-fe/")
        return source_ref

    @staticmethod
    def _source_ref_roots(raw_source_ref: str, source_ref: str) -> list[Path]:
        if raw_source_ref.startswith("open-agency/"):
            return [REPO_ROOT]
        if raw_source_ref.startswith("open-agency-fe/"):
            return [FRONTEND_ROOT] if FRONTEND_ROOT.exists() else []
        if raw_source_ref.startswith("agency/"):
            return [REPO_ROOT]
        if raw_source_ref.startswith("agency-fe/"):
            return [FRONTEND_ROOT] if FRONTEND_ROOT.exists() else []
        if FRONTEND_ROOT.exists():
            return [REPO_ROOT, FRONTEND_ROOT]
        if source_ref.startswith(FRONTEND_SOURCE_REF_PREFIXES):
            return []
        return [REPO_ROOT]

    @staticmethod
    def _is_external_or_frontend_endpoint(path: str) -> bool:
        return (
            path.startswith(FRONTEND_OR_EXTERNAL_ENDPOINT_PREFIXES)
            or path.startswith("/Users/")
            or path.startswith("/app/")
            or path in {"/memory", "/integrations", "/assistant", "/marketplace", "/profile"}
        )


if __name__ == "__main__":
    unittest.main()
