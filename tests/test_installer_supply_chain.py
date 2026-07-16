from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SHELL_INSTALLERS = [ROOT / "install" / "install-linux.sh", ROOT / "install" / "install-mac.sh"]
WINDOWS_INSTALLER = ROOT / "install" / "install-windows.ps1"
INSTALLERS = [*SHELL_INSTALLERS, WINDOWS_INSTALLER]
UNIX_LAUNCHER = ROOT / "scripts" / "launcher" / "run-unix.sh"


@pytest.mark.parametrize("script", [*SHELL_INSTALLERS, UNIX_LAUNCHER])
def test_install_shell_scripts_parse(script: Path) -> None:
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize("script", INSTALLERS)
def test_installers_require_content_addressed_revisions(script: Path) -> None:
    source = script.read_text()

    assert "AGENCY_BACKEND_COMMIT" in source
    assert "AGENCY_FRONTEND_COMMIT" in source
    assert "pull --ff-only" not in source
    assert "checkout --detach" in source
    assert "rev-parse --verify" in source
    if script.suffix == ".sh":
        assert 'if ! require_immutable_commit "${commit_name}" "${commit}"' in source
        assert '"${commit}^{commit}"' in source
    else:
        assert '"$Commit^{commit}"' in source


def test_installers_do_not_execute_mutable_bootstrap_scripts() -> None:
    source = "\n".join(script.read_text() for script in SHELL_INSTALLERS)

    assert "Homebrew/install/HEAD" not in source
    assert '/bin/bash -c "$(curl' not in source


def test_launcher_does_not_download_mutable_tunnel_binaries() -> None:
    source = UNIX_LAUNCHER.read_text()

    assert "ngrok-v3-stable" not in source
    assert "cloudflared/releases/latest" not in source
    assert "ngrok_download_url" not in source
    assert "cloudflared_download_url" not in source
    assert "set AGENCY_NGROK_BIN to a verified executable" in source
    assert "set AGENCY_CLOUDFLARE_TUNNEL_BIN to a verified executable" in source
