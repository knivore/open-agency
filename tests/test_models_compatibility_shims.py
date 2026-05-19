from __future__ import annotations

import unittest
from pathlib import Path


class LegacyFolderDeletionTests(unittest.TestCase):
    def test_deleted_legacy_folders_are_absent(self):
        repo_root = Path(__file__).resolve().parents[1]
        self.assertFalse((repo_root / "models").exists())
        self.assertFalse((repo_root / "routers").exists())
        self.assertFalse((repo_root / "tools_directory").exists())
        self.assertFalse((repo_root / "database" / "db_connection.py").exists())
        self.assertFalse((repo_root / "app" / "tools" / "legacy.py").exists())
        self.assertFalse((repo_root / "app" / "tools" / "legacy_catalog.py").exists())
        self.assertFalse((repo_root / "util").exists())
        self.assertFalse((repo_root / "util" / "process_manager.py").exists())
        self.assertFalse((repo_root / "util" / "logger_config.py").exists())
        self.assertFalse((repo_root / "util" / "aws_route53.py").exists())
