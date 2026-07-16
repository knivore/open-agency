from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_COMMON = REPO_ROOT / "scripts" / "launcher" / "common.sh"
WINDOWS_LAUNCHER = REPO_ROOT / "scripts" / "launcher" / "run-windows.sh"
UNIX_LAUNCHER = REPO_ROOT / "scripts" / "launcher" / "run-unix.sh"


class LauncherTunnelPreferenceTests(unittest.TestCase):
    def _run_common(
        self,
        script: str,
        *,
        env: dict[str, str],
        root_dir: Path | None = None,
    ) -> list[str]:
        bash = shutil.which("bash") or "bash"
        if os.name == "nt":
            git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
            if git_bash.is_file():
                bash = str(git_bash)
        repo_root = shlex.quote((root_dir or REPO_ROOT).as_posix())
        launcher_common = shlex.quote(LAUNCHER_COMMON.as_posix())
        command = f"""
ROOT_DIR={repo_root}
source {launcher_common}
_setup_status_python() {{ printf '%s\\n' "$PYTHON_BIN"; }}
{script}
"""
        result = subprocess.run(
            [bash, "-c", command],
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

    def test_tunnel_auto_install_is_shared_and_opt_out(self) -> None:
        output = self._run_common(
            """
for source in launcher browser cli; do
  AGENCY_TUNNEL_PREFERENCE_SOURCE="$source"
  if tunnel_auto_install_enabled; then
    printf '%s=enabled\\n' "$source"
  else
    printf '%s=disabled\\n' "$source"
  fi
done
export AGENCY_TUNNEL_AUTO_INSTALL=false
if tunnel_auto_install_enabled; then
  echo opt-out=enabled
else
  echo opt-out=disabled
fi
""",
            env={},
        )

        self.assertEqual(
            output,
            ["launcher=enabled", "browser=enabled", "cli=enabled", "opt-out=disabled"],
        )

    def test_relative_preference_path_is_workspace_relative(self) -> None:
        output = self._run_common(
            """
export AGENCY_TUNNEL_PREFERENCE_PATH=.agency/tunnel-preference.json
saved_tunnel_preference_path
""",
            env={},
        )

        self.assertEqual(output, [f"{REPO_ROOT.as_posix()}/.agency/tunnel-preference.json"])

    def test_dotenv_tunnel_settings_are_loaded_without_overwriting_cli_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(
                "AGENCY_PUBLIC_TUNNEL_PROVIDER=cloudflare\n"
                "AGENCY_CLOUDFLARE_TUNNEL_TOKEN=from-dotenv\n",
                encoding="utf-8",
            )
            output = self._run_common(
                """
export AGENCY_PUBLIC_TUNNEL_PROVIDER=ngrok
export AGENCY_TUNNEL_CLI_OVERRIDE=true
load_dotenv_preserving_cli_tunnel_overrides
printf '%s\\n' "$AGENCY_PUBLIC_TUNNEL_PROVIDER"
printf '%s\\n' "$AGENCY_CLOUDFLARE_TUNNEL_TOKEN"
""",
                env={},
                root_dir=root,
            )

        self.assertEqual(output, ["ngrok", "from-dotenv"])

    def test_windows_launcher_supports_configured_provider_binaries_and_provider_setup(self) -> None:
        source = WINDOWS_LAUNCHER.read_text(encoding="utf-8")

        self.assertIn('AGENCY_NGROK_BIN', source)
        self.assertIn('AGENCY_CLOUDFLARE_TUNNEL_BIN', source)
        self.assertIn('config add-authtoken', source)
        self.assertIn('ngrok_bin_path', source)
        self.assertIn('cloudflared_bin_path', source)
        self.assertIn('winget_bin_path', source)
        self.assertIn('AGENCY_CLOUDFLARE_TUNNEL_TOKEN', source)
        self.assertIn('tunnel --no-autoupdate --url', source)
        self.assertIn('tunnel --no-autoupdate run --token', source)
        self.assertIn('Ngrok.Ngrok', source)
        self.assertIn('Cloudflare.cloudflared', source)
        self.assertIn('--source winget', source)
        self.assertIn('AGENCY_TUNNEL_AUTO_INSTALL', source)

    def test_unix_launcher_supports_macos_provider_install_and_setup(self) -> None:
        source = UNIX_LAUNCHER.read_text(encoding="utf-8")
        common_source = LAUNCHER_COMMON.read_text(encoding="utf-8")

        self.assertIn('tunnel_auto_install_enabled', common_source)
        self.assertIn('brew_bin_path', source)
        self.assertIn('install_tunnel_provider', source)
        self.assertIn('install --cask ngrok', source)
        self.assertIn('install cloudflared', source)
        self.assertIn('refresh_brew_path', source)
        self.assertIn('load_dotenv_preserving_cli_tunnel_overrides', source)
        self.assertIn('AGENCY_TUNNEL_PREFERENCE_SOURCE', common_source)
        self.assertIn('AGENCY_TUNNEL_AUTO_INSTALL', common_source)


if __name__ == "__main__":
    unittest.main()
