from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.runtime.workspace_paths import default_repo_write_mounts


class WorkspacePathTests(unittest.TestCase):
    def test_repo_write_mounts_use_runtime_workspace_targets_when_present(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENCY_BACKEND_WORKSPACE": "/repo/backend",
                "AGENCY_FRONTEND_WORKSPACE": "/repo/frontend",
            },
            clear=False,
        ):
            mounts = default_repo_write_mounts()

        self.assertEqual(mounts[0]["target"], "/repo/backend")
        self.assertEqual(mounts[1]["target"], "/repo/frontend")

    def test_repo_write_mounts_fall_back_to_default_targets(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            mounts = default_repo_write_mounts()

        self.assertEqual(mounts[0]["repo"], "open-agency")
        self.assertEqual(mounts[0]["target"], "/workspace/open-agency")
        self.assertEqual(mounts[1]["repo"], "open-agency-fe")
        self.assertEqual(mounts[1]["target"], "/workspace/open-agency-fe")
