from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_MIN_FREE_BYTES = int(os.environ.get("BROWSER_RUNTIME_MIN_FREE_BYTES", str(256 * 1024 * 1024)))
_INITIALIZED_ROOTS: set[str] = set()


def _candidate_roots() -> list[Path]:
    configured = os.environ.get("BROWSER_RUNTIME_ROOT", "").strip()
    roots: list[Path] = []
    if configured:
        roots.append(Path(configured).expanduser())
    roots.extend(
        [
            Path.cwd() / ".browser-runtime",
            Path(tempfile.gettempdir()) / "browser-runtime",
        ]
    )
    return roots


def _has_enough_space(path: Path) -> bool:
    try:
        return shutil.disk_usage(path).free >= _MIN_FREE_BYTES
    except FileNotFoundError:
        return shutil.disk_usage(path.parent).free >= _MIN_FREE_BYTES
    except OSError:
        return False


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        return True
    except OSError:
        return False


def _resolve_runtime_root() -> Path:
    for root in _candidate_roots():
        resolved = root.resolve()
        if _has_enough_space(resolved) and _is_writable_dir(resolved):
            return resolved
    raise RuntimeError("No writable browser runtime directory with sufficient free space")


def configure_browser_runtime_env(app_name: str = "agency-browser-tool") -> str:
    root = (_resolve_runtime_root() / app_name).resolve()
    root_key = str(root)
    if root_key not in _INITIALIZED_ROOTS:
        root.mkdir(parents=True, exist_ok=True)
        _INITIALIZED_ROOTS.add(root_key)

    tmp_dir = root / "tmp"
    cache_dir = root / "cache"
    config_dir = root / "config"
    runtime_dir = root / "runtime"
    for path in (tmp_dir, cache_dir, config_dir, runtime_dir):
        path.mkdir(parents=True, exist_ok=True)
    runtime_dir.chmod(0o700)

    os.environ["TMPDIR"] = str(tmp_dir)
    os.environ["TMP"] = str(tmp_dir)
    os.environ["TEMP"] = str(tmp_dir)
    os.environ["XDG_CACHE_HOME"] = str(cache_dir)
    os.environ["XDG_CONFIG_HOME"] = str(config_dir)
    os.environ["XDG_RUNTIME_DIR"] = str(runtime_dir)
    os.environ.setdefault("NO_AT_BRIDGE", "1")
    os.environ.setdefault("GTK_A11Y", "none")

    for env_name in ("DBUS_SESSION_BUS_ADDRESS", "DBUS_SYSTEM_BUS_ADDRESS"):
        value = os.environ.get(env_name, "").strip()
        if value and not value.startswith(("unix:", "tcp:")):
            os.environ.pop(env_name, None)
    return str(root)


def ensure_browser_runtime_dir(app_name: str, *parts: str) -> str:
    root = Path(configure_browser_runtime_env(app_name))
    path = root.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


__all__ = ["configure_browser_runtime_env", "ensure_browser_runtime_dir"]
