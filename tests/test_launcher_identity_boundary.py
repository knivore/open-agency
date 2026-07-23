from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _function_body(source: str, function_name: str) -> str:
    start = source.index(f"{function_name}() {{")
    end = source.index("\n}\n", start)
    return source[start:end]


class LauncherIdentityBoundaryTests(unittest.TestCase):
    def test_unix_launcher_generates_identity_key_before_public_tunnel(self) -> None:
        source = (REPO_ROOT / "scripts/launcher/run-unix.sh").read_text(encoding="utf-8")
        generator = _function_body(source, "ensure_internal_api_key")
        browser_generator = _function_body(source, "ensure_browser_runtime_signing_secret")
        env_setup = _function_body(source, "ensure_host_backend_env_files")
        startup = _function_body(source, "start_background")

        self.assertIn("secrets.token_urlsafe(48)", generator)
        self.assertIn("secrets.token_urlsafe(48)", browser_generator)
        self.assertIn("BROWSER_RUNTIME_SIGNING_SECRET", browser_generator)
        self.assertIn('${#browser_secret}', browser_generator)
        self.assertIn("ensure_internal_api_key", env_setup)
        self.assertIn("ensure_browser_runtime_signing_secret", env_setup)
        self.assertLess(startup.index("ensure_host_backend_env_files"), startup.index("start_public_tunnel"))
        self.assertIn("AGENCY_BACKEND_INTERNAL_API_KEY", source)

    def test_windows_launcher_generates_identity_key_before_public_tunnel(self) -> None:
        source = (REPO_ROOT / "scripts/launcher/run-windows.sh").read_text(encoding="utf-8")
        generator = _function_body(source, "ensure_internal_api_key")
        browser_generator = _function_body(source, "ensure_browser_runtime_signing_secret")
        startup = _function_body(source, "start_background")

        self.assertIn("secrets.token_urlsafe(48)", generator)
        self.assertIn("secrets.token_urlsafe(48)", browser_generator)
        self.assertIn("BROWSER_RUNTIME_SIGNING_SECRET", browser_generator)
        self.assertIn('${#browser_secret}', browser_generator)
        self.assertLess(
            startup.index("ensure_browser_runtime_signing_secret"),
            startup.index("start_public_tunnel"),
        )
        self.assertLess(startup.index("ensure_internal_api_key"), startup.index("start_public_tunnel"))
        self.assertIn("AGENCY_BACKEND_INTERNAL_API_KEY", source)


if __name__ == "__main__":
    unittest.main()
