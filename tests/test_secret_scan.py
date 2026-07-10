"""Tests for the local secret scan helper."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "scan_secrets.py"
SPEC = importlib.util.spec_from_file_location("scan_secrets", MODULE_PATH)
assert SPEC and SPEC.loader
scan_secrets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan_secrets)


class SecretScanTests(unittest.TestCase):
    def test_placeholder_env_value_is_ignored(self) -> None:
        findings = scan_secrets.scan_text(Path(".env.example"), "NEXTAUTH_SECRET=replace-me-in-local-dev\n")
        self.assertEqual(findings, [])

    def test_real_env_value_is_flagged(self) -> None:
        findings = scan_secrets.scan_text(Path(".env"), "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456\n")
        self.assertEqual([finding.rule for finding in findings], ["openai-key"])

    def test_allow_marker_skips_line(self) -> None:
        findings = scan_secrets.scan_text(
            Path(".env"),
            "AGENCY_OPERATOR_TOKEN=super-secret-value secret-scan: allow\n",
        )
        self.assertEqual(findings, [])

    def test_scan_repo_includes_local_env_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            (repo_root / ".git").mkdir()
            (repo_root / ".env.local").write_text(
                "AGENCY_INTERNAL_API_KEY=ghp_abcdefghijklmnopqrstuvwxyz123456\n",
                encoding="utf-8",
            )
            findings = scan_secrets.scan_repo(repo_root)

        self.assertEqual([finding.rule for finding in findings], ["github-pat"])


if __name__ == "__main__":
    unittest.main()
