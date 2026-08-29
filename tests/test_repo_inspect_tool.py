from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import get_settings
from app.tools.implementations.custom.repo_inspect import inspect_repo


class RepoInspectToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="agency-repo-inspect-"))
        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))
        self.previous_allowed = os.environ.get("SANDBOX_EDIT_ALLOWED_REPOS")
        self.addCleanup(self._restore_allowed_repos)

    def test_inspect_repo_returns_git_and_query_signals(self) -> None:
        repo = self._create_repo("agency")
        (repo / "README.md").write_text("# Agency\n\nTODO: tighten run summaries\n", encoding="utf-8")
        docs_dir = repo / "docs"
        docs_dir.mkdir()
        (docs_dir / "architecture.md").write_text(
            "Decision journal entries explain architecture tradeoffs.\n",
            encoding="utf-8",
        )
        self._git(repo, "add", "README.md", "docs/architecture.md")
        self._git(repo, "commit", "-m", "seed repo")

        os.environ["SANDBOX_EDIT_ALLOWED_REPOS"] = str(repo)
        get_settings.cache_clear()

        result = inspect_repo(repo="agency", query="Decision journal", max_files=12, max_hits=12)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["repo_id"], "agency")
        self.assertEqual(result["repo_path"], str(repo))
        self.assertTrue(result["branch"])
        self.assertGreaterEqual(result["tracked_file_count"], 2)
        self.assertTrue(any(hit["path"] == "README.md" for hit in result["todo_hits"]))
        self.assertTrue(any(hit["path"] == "docs/architecture.md" for hit in result["query_hits"]))
        self.assertTrue(any(item["path"] == "README.md" for item in result["file_excerpts"]))

    def test_inspect_repo_rejects_unknown_repo(self) -> None:
        repo = self._create_repo("agency")
        os.environ["SANDBOX_EDIT_ALLOWED_REPOS"] = str(repo)
        get_settings.cache_clear()
        with self.assertRaisesRegex(ValueError, "Unknown repo"):
            inspect_repo(repo="agency-fe")

    def test_inspect_repo_uses_runtime_workspace_when_configured_allowlist_is_unavailable(self) -> None:
        repo = self._create_repo("agency")
        (repo / "README.md").write_text("# Mounted Agency\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "seed mounted repo")

        os.environ["SANDBOX_EDIT_ALLOWED_REPOS"] = str(self.temp_dir / "missing-repo")
        with patch.dict(os.environ, {"AGENCY_BACKEND_WORKSPACE": str(repo)}, clear=False):
            get_settings.cache_clear()
            result = inspect_repo(repo="agency")
        get_settings.cache_clear()

        self.assertEqual(result["repo_id"], "agency")
        self.assertEqual(result["repo_path"], str(repo.resolve()))

    def test_inspect_repo_uses_runtime_workspace_when_configured_allowlist_is_unavailable(self) -> None:
        repo = self._create_repo("agency")
        (repo / "README.md").write_text("# Mounted Agency\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "seed mounted repo")

        os.environ["SANDBOX_EDIT_ALLOWED_REPOS"] = str(self.temp_dir / "missing-repo")
        with patch.dict(os.environ, {"AGENCY_BACKEND_WORKSPACE": str(repo)}, clear=False):
            get_settings.cache_clear()
            result = inspect_repo(repo="agency")
        get_settings.cache_clear()

        self.assertEqual(result["repo_id"], "agency")
        self.assertEqual(result["repo_path"], str(repo.resolve()))

    def test_inspect_repo_collects_hits_beyond_max_files_preview(self) -> None:
        repo = self._create_repo("scan-target")
        app_dir = repo / "app"
        app_dir.mkdir()
        for index in range(30):
            marker = ""
            if index == 29:
                marker = "\n# TODO: inspect late file\nTARGET_MARKER = 'daily-brief'\n"
            (app_dir / f"{index:03}.py").write_text(
                f"def file_{index:03}() -> int:\n    return {index}\n{marker}",
                encoding="utf-8",
            )
        self._git(repo, "add", "app")
        self._git(repo, "commit", "-m", "seed scan target")

        os.environ["SANDBOX_EDIT_ALLOWED_REPOS"] = str(repo)
        get_settings.cache_clear()

        result = inspect_repo(repo="scan-target", query="daily-brief", max_files=24, max_hits=12)

        self.assertEqual(len(result["scanned_files"]), 24)
        self.assertNotIn("app/029.py", result["scanned_files"])
        self.assertTrue(any(hit["path"] == "app/029.py" for hit in result["todo_hits"]))
        self.assertTrue(any(hit["path"] == "app/029.py" for hit in result["query_hits"]))

    def test_inspect_repo_always_returns_focused_file_excerpt(self) -> None:
        repo = self._create_repo("focused-target")
        app_dir = repo / "app"
        target = app_dir / "api" / "physical-devices" / "[deviceId]" / "state" / "route.ts"
        target.parent.mkdir(parents=True)
        (repo / "README.md").write_text("TODO: unrelated repository cleanup\n", encoding="utf-8")
        target.write_text("export const graph = 'stream';\n", encoding="utf-8")
        self._git(repo, "add", "README.md", "app/api/physical-devices/[deviceId]/state/route.ts")
        self._git(repo, "commit", "-m", "seed focused target")

        os.environ["SANDBOX_EDIT_ALLOWED_REPOS"] = str(repo)
        get_settings.cache_clear()

        result = inspect_repo(repo="focused-target", focus_paths=["app/api/physical-devices/[deviceId]/state/route.ts"], max_files=1, max_hits=12)

        self.assertEqual(result["scanned_files"], ["app/api/physical-devices/[deviceId]/state/route.ts"])
        self.assertEqual([item["path"] for item in result["file_excerpts"]][0], "app/api/physical-devices/[deviceId]/state/route.ts")
        self.assertIn("export const graph", result["file_excerpts"][0]["excerpt"])
    def _create_repo(self, name: str) -> Path:
        repo = self.temp_dir / name
        repo.mkdir()
        self._git(repo, "init")
        self._git(repo, "config", "user.email", "tests@example.com")
        self._git(repo, "config", "user.name", "Agency Tests")
        return repo

    def _git(self, repo: Path, *args: str) -> None:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr.strip() or completed.stdout.strip() or f"git {' '.join(args)} failed")

    def _restore_allowed_repos(self) -> None:
        if self.previous_allowed is None:
            os.environ.pop("SANDBOX_EDIT_ALLOWED_REPOS", None)
        else:
            os.environ["SANDBOX_EDIT_ALLOWED_REPOS"] = self.previous_allowed
        get_settings.cache_clear()


if __name__ == "__main__":
    unittest.main()
