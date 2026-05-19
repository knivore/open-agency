from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.coding_agent.codex_runner import run_codex_job
from app.coding_agent.git_tools import get_git_diff, get_git_status, run_git
from app.coding_agent.jobs import create_coding_job
from app.coding_agent.test_runner import run_command
from app.coding_agent.workspaces import BACKEND_WORKSPACE, WorkspaceResolutionError, resolve_task_file, resolve_workspace


class CodingAgentWorkspaceTests(unittest.TestCase):
    def test_resolve_workspace_accepts_backend_alias_and_exact_path(self) -> None:
        self.assertEqual(resolve_workspace("agency"), BACKEND_WORKSPACE)
        self.assertEqual(resolve_workspace(str(BACKEND_WORKSPACE)), BACKEND_WORKSPACE)

    def test_resolve_workspace_rejects_parent_traversal_and_missing_paths(self) -> None:
        with self.assertRaises(WorkspaceResolutionError):
            resolve_workspace("../agency")
        with self.assertRaises(WorkspaceResolutionError):
            resolve_workspace("/path/that/does/not/exist")

    def test_resolve_workspace_accepts_existing_non_alias_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            self.assertEqual(resolve_workspace(temp_dir), Path(temp_dir).resolve())

    def test_resolve_task_file_requires_markdown_in_allowed_workspace(self) -> None:
        with tempfile.TemporaryDirectory(dir=BACKEND_WORKSPACE) as temp_dir:
            task = Path(temp_dir) / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            self.assertEqual(resolve_task_file(task), task.resolve())

        with self.assertRaises(WorkspaceResolutionError):
            resolve_task_file(BACKEND_WORKSPACE / ".env")


class CodingAgentRunnerTests(unittest.TestCase):
    def test_create_coding_job_writes_task_markdown_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            job = create_coding_job(
                title="Add feature",
                description="Implement a small feature.",
                workspace="agency",
                requested_by="user-1",
                original_request="Please add the feature.",
                job_root=temp_dir,
                job_id="job-1",
            )

            task = Path(job.task_md_path)
            self.assertTrue(task.exists())
            self.assertIn("## Constraints", task.read_text(encoding="utf-8"))
            self.assertTrue((Path(temp_dir) / "job-1" / "job.json").exists())
            self.assertEqual(job.status, "queued")
            self.assertEqual(job.workspace, str(BACKEND_WORKSPACE))

    def test_run_codex_job_uses_list_command_and_workspace_write_sandbox(self) -> None:
        with tempfile.TemporaryDirectory(dir=BACKEND_WORKSPACE) as temp_dir:
            task = Path(temp_dir) / "task.md"
            task.write_text("# Task\n", encoding="utf-8")
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="done", stderr="")

            with patch("app.coding_agent.codex_runner.shutil.which", return_value="/usr/local/bin/codex"), patch(
                "app.coding_agent.codex_runner.subprocess.run", return_value=completed
            ) as run:
                result = run_codex_job("agency", task, timeout_seconds=60)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "done")
        called_command = run.call_args.args[0]
        self.assertEqual(called_command[:4], ["/usr/local/bin/codex", "exec", "--sandbox", "workspace-write"])
        self.assertEqual(run.call_args.kwargs["cwd"], BACKEND_WORKSPACE)
        self.assertFalse(run.call_args.kwargs["check"])


class CodingAgentGitAndTestRunnerTests(unittest.TestCase):
    def test_git_status_and_diff_are_read_only(self) -> None:
        self.assertIsInstance(get_git_status("agency"), str)
        self.assertIsInstance(get_git_diff("agency"), str)
        with self.assertRaises(Exception):
            run_git("agency", ["push"])

    def test_run_command_uses_argument_list(self) -> None:
        output = run_command("agency", ["git", "status", "--short"], timeout_seconds=120)
        self.assertIsInstance(output, str)


if __name__ == "__main__":
    unittest.main()
