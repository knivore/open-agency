from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_COMMON = REPO_ROOT / "scripts" / "launcher" / "common.sh"


class LauncherTunnelPreferenceTests(unittest.TestCase):
    def _run_common(self, script: str, *, env: dict[str, str]) -> list[str]:
        command = f"""
ROOT_DIR={REPO_ROOT!s}
source {LAUNCHER_COMMON!s}
_setup_status_python() {{ printf '%s\\n' "$PYTHON_BIN"; }}
{script}
"""
        result = subprocess.run(
            ["bash", "-c", command],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHON_BIN": sys.executable, **env},
        )
        return result.stdout.strip().splitlines()

    def test_saved_browser_preference_supplies_default_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            preference_path = Path(temp_dir) / "tunnel-preference.json"
            preference_path.write_text(
                json.dumps(
                    {
                        "provider": "cloudflare",
                        "custom_domain": "agency.example.com",
                        "source": "browser",
                    }
                ),
                encoding="utf-8",
            )

            output = self._run_common(
                """
export AGENCY_PUBLIC_TUNNEL_PROVIDER=ngrok
apply_saved_or_detected_tunnel_preference
printf '%s\\n' "$AGENCY_PUBLIC_TUNNEL_PROVIDER"
printf '%s\\n' "$AGENCY_TUNNEL_CUSTOM_DOMAIN"
printf '%s\\n' "$AGENCY_TUNNEL_PREFERENCE_SOURCE"
""",
                env={"AGENCY_TUNNEL_PREFERENCE_PATH": str(preference_path)},
            )

        self.assertEqual(output, ["cloudflare", "agency.example.com", "browser"])

    def test_cli_provider_overrides_saved_browser_preference_for_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            preference_path = Path(temp_dir) / "tunnel-preference.json"
            preference_path.write_text(
                json.dumps(
                    {
                        "provider": "cloudflare",
                        "custom_domain": "agency.example.com",
                        "source": "browser",
                    }
                ),
                encoding="utf-8",
            )

            output = self._run_common(
                """
parse_cli start -ngrok --domain launch.example.com
apply_saved_or_detected_tunnel_preference
printf '%s\\n' "$AGENCY_PUBLIC_TUNNEL_PROVIDER"
printf '%s\\n' "$AGENCY_TUNNEL_CUSTOM_DOMAIN"
printf '%s\\n' "$AGENCY_TUNNEL_PREFERENCE_SOURCE"
""",
                env={"AGENCY_TUNNEL_PREFERENCE_PATH": str(preference_path)},
            )

        self.assertEqual(output, ["ngrok", "launch.example.com", "cli"])

    def test_auto_provider_prefers_configured_cloudflare_without_saved_preference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = self._run_common(
                """
export AGENCY_PUBLIC_TUNNEL_PROVIDER=auto
apply_saved_or_detected_tunnel_preference
printf '%s\\n' "$AGENCY_PUBLIC_TUNNEL_PROVIDER"
printf '%s\\n' "$AGENCY_TUNNEL_PREFERENCE_SOURCE"
""",
                env={
                    "AGENCY_TUNNEL_PREFERENCE_PATH": str(Path(temp_dir) / "missing.json"),
                    "AGENCY_CLOUDFLARE_TUNNEL_TOKEN": "configured-token",
                },
            )

        self.assertEqual(output, ["cloudflare", "launcher"])

    def test_auto_provider_defaults_to_cloudflare_for_fresh_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = self._run_common(
                """
export AGENCY_PUBLIC_TUNNEL_PROVIDER=auto
apply_saved_or_detected_tunnel_preference
printf '%s\\n' "$AGENCY_PUBLIC_TUNNEL_PROVIDER"
printf '%s\\n' "$AGENCY_TUNNEL_PREFERENCE_SOURCE"
""",
                env={
                    "AGENCY_TUNNEL_PREFERENCE_PATH": str(Path(temp_dir) / "missing.json"),
                    "AGENCY_CLOUDFLARE_TUNNEL_TOKEN": "",
                    "AGENCY_NGROK_AUTHTOKEN": "",
                    "HOME": temp_dir,
                    "PATH": "/usr/bin:/bin",
                },
            )

        self.assertEqual(output, ["cloudflare", "launcher"])

    def test_saved_blank_domain_clears_launcher_custom_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            preference_path = Path(temp_dir) / "tunnel-preference.json"
            preference_path.write_text(
                json.dumps(
                    {
                        "provider": "ngrok",
                        "custom_domain": None,
                        "source": "browser",
                    }
                ),
                encoding="utf-8",
            )

            output = self._run_common(
                """
export AGENCY_PUBLIC_TUNNEL_PROVIDER=cloudflare
export AGENCY_TUNNEL_CUSTOM_DOMAIN=old.example.com
apply_saved_or_detected_tunnel_preference
printf '%s\\n' "$AGENCY_PUBLIC_TUNNEL_PROVIDER"
printf '<%s>\\n' "$AGENCY_TUNNEL_CUSTOM_DOMAIN"
""",
                env={"AGENCY_TUNNEL_PREFERENCE_PATH": str(preference_path)},
            )

        self.assertEqual(output, ["ngrok", "<>"])


if __name__ == "__main__":
    unittest.main()
