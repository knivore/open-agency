"""Repository hygiene checks for tracked local-only artifacts."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[1]


class RepoHygieneTests(unittest.TestCase):
    def test_git_does_not_track_local_only_artifacts(self) -> None:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            check=True,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tracked_paths = [path for path in result.stdout.decode("utf-8").split("\0") if path]
        violations = sorted(path for path in tracked_paths if self._is_blocked_artifact(path))

        self.assertEqual(
            violations,
            [],
            "Tracked local-only artifacts must stay out of git: "
            "database_exports/*.dump, database_exports/*.json, .env, logs/, .logs/, *.log",
        )

    @staticmethod
    def _is_blocked_artifact(path: str) -> bool:
        candidate = PurePosixPath(path)
        path_text = candidate.as_posix()
        return any(
            (
                candidate.match("database_exports/*.dump"),
                candidate.match("database_exports/*.json"),
                candidate.name == ".env",
                path_text.startswith("logs/"),
                path_text.startswith(".logs/"),
                candidate.suffix == ".log",
            )
        )
