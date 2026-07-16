"""Persistent setup and readiness management for the optional OpenVoice capability."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


AGENCY_ROOT = Path(__file__).resolve().parents[2]
OPENVOICE_COMMIT = "74a1d147b17a8c3092dd5430504bd83ef6c7eb23"
OPENVOICE_VOICES = (
    "friendly",
    "default",
    "cheerful",
    "excited",
    "whispering",
    "shouting",
    "sad",
    "angry",
    "terrified",
)


def _workspace_root() -> Path:
    return Path(os.getenv("AGENCY_BACKEND_WORKSPACE", str(AGENCY_ROOT))).expanduser()


def _openvoice_root() -> Path:
    default = AGENCY_ROOT / "external" / "openvoice"
    return Path(os.getenv("AGENCY_OPENVOICE_ROOT", str(default))).expanduser()


def _checkpoints_dir() -> Path:
    default = _openvoice_root() / "checkpoints"
    return Path(os.getenv("AGENCY_OPENVOICE_CHECKPOINTS_DIR", str(default))).expanduser()


def _settings_path() -> Path:
    default = _workspace_root() / ".agency" / "openvoice-settings.json"
    return Path(os.getenv("AGENCY_OPENVOICE_SETTINGS_PATH", str(default))).expanduser()


@dataclass(frozen=True, slots=True)
class OpenVoiceCheckpoint:
    relative_path: str
    sha256: str

    @property
    def url(self) -> str:
        return f"https://huggingface.co/myshell-ai/OpenVoice/resolve/main/checkpoints/{self.relative_path}"


OPENVOICE_CHECKPOINTS = (
    OpenVoiceCheckpoint(
        "base_speakers/EN/config.json",
        "f01c2ecbf115128d00e13acc57eadb89ac0579635c1a27c7957158bceb77e561",
    ),
    OpenVoiceCheckpoint(
        "base_speakers/EN/checkpoint.pth",
        "1db1ae1a5c8ded049bd1536051489aefbfad4a5077c01c2257e9e88fa1bb8422",
    ),
    OpenVoiceCheckpoint(
        "base_speakers/EN/en_default_se.pth",
        "9cab24002eec738d0fe72cb73a34e57fbc3999c1bd4a1670a7b56ee4e3590ac9",
    ),
    OpenVoiceCheckpoint(
        "converter/config.json",
        "86c61ff1ac3efb9fbf0246c727345793b34053fb1dd5b98e7f561201d4f90739",
    ),
    OpenVoiceCheckpoint(
        "converter/checkpoint.pth",
        "89ae83aa4e3668fef64b388b789ff7b0ce0def9f801069edfc18a00ea420748d",
    ),
)


@dataclass(slots=True)
class OpenVoiceSetupService:
    openvoice_root: Path = field(default_factory=_openvoice_root)
    checkpoints_dir: Path = field(default_factory=_checkpoints_dir)
    settings_path: Path = field(default_factory=_settings_path)

    def get_settings(self) -> dict[str, Any]:
        default = {"default_voice": "friendly", "language": "English"}
        if not self.settings_path.is_file():
            return default
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        voice = str(payload.get("default_voice") or "friendly").strip().lower()
        return {
            "default_voice": voice if voice in OPENVOICE_VOICES else "friendly",
            "language": "English",
        }

    def save_settings(self, *, default_voice: str) -> dict[str, Any]:
        normalized = default_voice.strip().lower()
        if normalized not in OPENVOICE_VOICES:
            raise ValueError(f"Unsupported OpenVoice preset '{default_voice}'.")
        payload = {"default_voice": normalized, "language": "English"}
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.settings_path)
        return payload

    def status(self) -> dict[str, Any]:
        paths = {item.relative_path: self.checkpoints_dir / item.relative_path for item in OPENVOICE_CHECKPOINTS}
        missing = [relative for relative, path in paths.items() if not path.is_file()]
        builtin_required = {"base_speakers/EN/config.json", "base_speakers/EN/checkpoint.pth"}
        cloning_required = set(paths)
        runtime_python = self.openvoice_root / ".venv" / "bin" / "python"
        return {
            "optional": True,
            "ready": runtime_python.is_file() and not (builtin_required & set(missing)),
            "supports_cloning": runtime_python.is_file() and not (cloning_required & set(missing)),
            "runtime": {
                "installed": runtime_python.is_file(),
                "root": str(self.openvoice_root),
                "revision": OPENVOICE_COMMIT,
            },
            "checkpoints": {
                "directory": str(self.checkpoints_dir),
                "installed": not (builtin_required & set(missing)),
                "missing_files": missing,
            },
            "settings": self.get_settings(),
            "available_voices": list(OPENVOICE_VOICES),
        }

    async def install_checkpoints(self, *, force: bool = False) -> dict[str, Any]:
        if not (self.openvoice_root / ".venv" / "bin" / "python").is_file():
            raise ValueError("OpenVoice runtime is not installed. Rebuild the Agency backend image first.")

        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(follow_redirects=True, timeout=600.0) as client:
            for checkpoint in OPENVOICE_CHECKPOINTS:
                destination = self.checkpoints_dir / checkpoint.relative_path
                if destination.is_file() and not force and _sha256_file(destination) == checkpoint.sha256:
                    continue
                await _download_checkpoint(client, checkpoint, destination)
        return self.status()


def get_openvoice_default_voice() -> str:
    return str(OpenVoiceSetupService().get_settings()["default_voice"])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _download_checkpoint(
        client: httpx.AsyncClient,
        checkpoint: OpenVoiceCheckpoint,
        destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    try:
        async with client.stream("GET", checkpoint.url) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    handle.write(chunk)
                    digest.update(chunk)
        if digest.hexdigest() != checkpoint.sha256:
            raise ValueError(f"Checksum verification failed for {checkpoint.relative_path}.")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["OPENVOICE_VOICES", "OpenVoiceSetupService", "get_openvoice_default_voice"]
