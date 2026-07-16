from __future__ import annotations

import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


class WindowsLauncherFrontendFallbackTests(unittest.TestCase):
    def test_compose_frontend_keeps_source_read_only_and_state_in_volumes(self) -> None:
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        frontend = compose["services"]["frontend"]

        self.assertIn("container-frontend", frontend["profiles"])
        mounts = {mount["target"]: mount for mount in frontend["volumes"]}
        self.assertTrue(mounts["/workspace/open-agency-fe"]["read_only"])
        self.assertEqual(mounts["/workspace/open-agency-fe/node_modules"]["type"], "volume")
        self.assertEqual(mounts["/workspace/open-agency-fe/.next"]["type"], "volume")
        self.assertTrue(mounts["/workspace/open-agency-fe/.env.local"]["read_only"])

    def test_compose_frontend_reuses_dependencies_until_runtime_or_lock_changes(self) -> None:
        compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        command = compose["services"]["frontend"]["command"][2]

        self.assertIn("sha256sum package-lock.json", command)
        self.assertIn("node --version", command)
        self.assertIn("npm ci", command)
        self.assertIn(".agency-package-lock-sha", command)

    def test_cmd_streams_output_while_preserving_the_launcher_log(self) -> None:
        launcher = (REPO_ROOT / "run-windows.cmd").read_text(encoding="utf-8")

        self.assertIn('set "AGENCY_RUN_LOG=%RUN_LOG%"', launcher)
        self.assertNotIn('%* > "%RUN_LOG%" 2>&1', launcher)

    def test_shell_launcher_uses_container_fallback_without_acl_changes(self) -> None:
        launcher = (REPO_ROOT / "scripts" / "launcher" / "run-windows.sh").read_text(encoding="utf-8")

        self.assertIn("start_container_frontend", launcher)
        self.assertIn('AGENCY_FRONTEND_RUNTIME:-auto', launcher)
        self.assertIn("Frontend source is read-only; using the automatic Docker frontend runtime.", launcher)
        self.assertNotIn("icacls", launcher.lower())


if __name__ == "__main__":
    unittest.main()
