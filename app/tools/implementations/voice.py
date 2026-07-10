from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from app.core import storage
from app.runtime.native.errors import ToolExecutionError
from app.tools.implementations.media import publish_media


VoiceProvider = Literal["auto", "system_tts", "openvoice_local"]
AGENCY_ROOT = Path(__file__).resolve().parents[3]
OPENVOICE_ROOT = Path(os.getenv("AGENCY_OPENVOICE_ROOT", str(AGENCY_ROOT / "external" / "openvoice"))).expanduser()
OPENVOICE_CHECKPOINTS_DIR = Path(
    os.getenv("AGENCY_OPENVOICE_CHECKPOINTS_DIR", str(OPENVOICE_ROOT / "checkpoints"))
).expanduser()
OPENVOICE_TIMEOUT_SECONDS = int(os.getenv("AGENCY_OPENVOICE_TIMEOUT_SECONDS", "300"))


class VoiceGenerateInput(BaseModel):
    text: str = Field(description="Text to synthesize into spoken audio.")
    provider: VoiceProvider = Field(
        default="auto",
        description="Voice provider. auto uses openvoice_local when a consented reference voice is supplied.",
    )
    voice: str | None = Field(default=None, description="Provider-specific voice preset or local OS voice name.")
    reference_voice_path: str | None = Field(
        default=None,
        description="Reference voice path for local OpenVoice generation.",
    )
    output_name: str | None = Field(
        default=None,
        description="Optional output filename or storage-key suffix. Path traversal and absolute paths are rejected.",
    )
    storage_key_prefix: str = Field(default="voice", description="Agency storage prefix for the generated audio.")
    purpose: str | None = Field(default=None, description="Human-readable purpose for audit and policy review.")
    ai_disclosure: bool = Field(
        default=False,
        description="Must be true so generated speech can be disclosed as AI-generated when delivered.",
    )
    consent_confirmed: bool = Field(
        default=False,
        description="Must be true when a reference or cloned voice provider is used.",
    )
    dry_run: bool = Field(default=True, description="Preview provider, guardrails, and storage target without synthesis.")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Optional workflow or delivery metadata.")

    @model_validator(mode="after")
    def validate_voice_policy(self) -> "VoiceGenerateInput":
        if not self.text.strip():
            raise ValueError("text is required.")
        if not self.ai_disclosure:
            raise ValueError("ai_disclosure must be true for generated voice output.")
        if self.provider == "openvoice_local" or self.reference_voice_path:
            if not self.consent_confirmed:
                raise ValueError("consent_confirmed must be true for reference or cloned voice generation.")
            if not (self.reference_voice_path or "").strip():
                raise ValueError("reference_voice_path is required for openvoice_local voice generation.")
        _safe_output_name(self.output_name)
        return self


def generate_voice(
        *,
        text: str,
        provider: str = "auto",
        voice: str | None = None,
        reference_voice_path: str | None = None,
        output_name: str | None = None,
        storage_key_prefix: str = "voice",
        purpose: str | None = None,
        ai_disclosure: bool = False,
        consent_confirmed: bool = False,
        dry_run: bool = True,
        metadata: dict[str, Any] | None = None,
        tool_context=None,  # noqa: ANN001
) -> dict[str, Any]:
    request = VoiceGenerateInput.model_validate(
        {
            "text": text,
            "provider": provider,
            "voice": voice,
            "reference_voice_path": reference_voice_path,
            "output_name": output_name,
            "storage_key_prefix": storage_key_prefix,
            "purpose": purpose,
            "ai_disclosure": ai_disclosure,
            "consent_confirmed": consent_confirmed,
            "dry_run": dry_run,
            "metadata": metadata or {},
        }
    )
    resolved_provider = _resolve_provider(request)
    storage_key = _storage_key_for(request, suffix=_default_suffix_for_provider(resolved_provider))
    base = _base_result(request, provider=resolved_provider, storage_key=storage_key)
    if request.dry_run:
        return {
            **base,
            "status": "preview",
            "warnings": _provider_warnings(request.provider, resolved_provider),
            "next_step": "Run again with dry_run=false to synthesize and publish a reusable voice artifact for any downstream workflow or delivery tool.",
        }

    if not storage.is_local_environment():
        return {
            **base,
            "status": "setup_required",
            "warnings": ["Local-first voice generation requires ENVIRONMENT=local so audio stays in local storage."],
            "setup": {"required": ["ENVIRONMENT=local", "LOCAL_STORAGE_PATH"], "local_first": True},
        }

    if resolved_provider == "system_tts":
        return _generate_with_system_tts(request, storage_key=storage_key)
    if resolved_provider == "openvoice_local":
        return _generate_with_openvoice_local(request, storage_key=storage_key)

    return {
        **base,
        "status": "setup_required",
        "warnings": _provider_warnings(request.provider, resolved_provider),
        "setup": _setup_instructions(resolved_provider),
    }


def _generate_with_system_tts(request: VoiceGenerateInput, *, storage_key: str) -> dict[str, Any]:
    command = _system_tts_command()
    if command is None:
        return {
            **_base_result(request, provider="system_tts", storage_key=storage_key),
            "status": "setup_required",
            "warnings": ["No local system TTS binary found. Install espeak/espeak-ng or use macOS say."],
            "setup": _setup_instructions("system_tts"),
        }

    binary, output_suffix = command
    storage_key = _replace_suffix(storage_key, output_suffix)
    content_type = mimetypes.guess_type(f"output{output_suffix}")[0] or "audio/wav"
    with tempfile.TemporaryDirectory() as temp_dir:
        output_path = Path(temp_dir) / f"agency-voice-{uuid4().hex}{output_suffix}"
        # Run local TTS directly without shell expansion so text cannot alter the command boundary.
        args = _system_tts_args(binary, output_path, request)
        completed = subprocess.run(args, check=False, capture_output=True, text=True, timeout=90)
        if completed.returncode != 0:
            raise ToolExecutionError(
                f"Local system TTS failed with exit code {completed.returncode}: {completed.stderr.strip()}"
            )
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise ToolExecutionError("Local system TTS did not produce an audio file.")
        published = publish_media(
            file_path=str(output_path),
            storage_key=storage_key,
            content_type=content_type,
            dry_run=False,
            metadata={
                **request.metadata,
                "voice_provider": "system_tts",
                "purpose": request.purpose,
                "ai_disclosure": request.ai_disclosure,
            },
        )
    return {
        **_base_result(request, provider="system_tts", storage_key=published["storage_key"]),
        "status": "generated",
        "file_path": storage.get_local_file_path(published["storage_key"]),
        "storage_uri": published["storage_uri"],
        "media_url": published["media_url"],
        "content_type": published["content_type"],
        "provider_fetchable": published["provider_fetchable"],
        "warnings": published["warnings"],
        "next_step": "Use file_path or media_url directly, or pass them to a delivery workflow/tool when the voice should be sent to a tied application.",
    }


def _generate_with_openvoice_local(request: VoiceGenerateInput, *, storage_key: str) -> dict[str, Any]:
    readiness = _openvoice_readiness(request)
    if readiness:
        return {
            **_base_result(request, provider="openvoice_local", storage_key=_replace_suffix(storage_key, ".wav")),
            "status": "setup_required",
            "warnings": readiness,
            "setup": _setup_instructions("openvoice_local"),
        }

    storage_key = _replace_suffix(storage_key, ".wav")
    with tempfile.TemporaryDirectory() as temp_dir:
        output_path = Path(temp_dir) / f"agency-openvoice-{uuid4().hex}.wav"
        # OpenVoice runs as a local subprocess so Agency owns the provider boundary without
        # coupling workflow execution to a separate media service.
        args = _openvoice_args(request, output_path)
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=OPENVOICE_TIMEOUT_SECONDS,
            cwd=str(OPENVOICE_ROOT),
            env=_openvoice_env(),
        )
        if completed.returncode != 0:
            raise ToolExecutionError(
                f"Local OpenVoice failed with exit code {completed.returncode}: {completed.stderr.strip()}"
            )
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise ToolExecutionError("Local OpenVoice did not produce an audio file.")
        published = publish_media(
            file_path=str(output_path),
            storage_key=storage_key,
            content_type="audio/wav",
            dry_run=False,
            metadata={
                **request.metadata,
                "voice_provider": "openvoice_local",
                "reference_voice_path": request.reference_voice_path,
                "purpose": request.purpose,
                "ai_disclosure": request.ai_disclosure,
                "consent_confirmed": request.consent_confirmed,
            },
        )
    return {
        **_base_result(request, provider="openvoice_local", storage_key=published["storage_key"]),
        "status": "generated",
        "file_path": storage.get_local_file_path(published["storage_key"]),
        "storage_uri": published["storage_uri"],
        "media_url": published["media_url"],
        "content_type": published["content_type"],
        "provider_fetchable": published["provider_fetchable"],
        "warnings": published["warnings"],
        "setup": {"openvoice_root": str(OPENVOICE_ROOT), "openvoice_checkpoints_dir": str(OPENVOICE_CHECKPOINTS_DIR)},
        "next_step": "Use file_path or media_url directly, or pass them to a delivery workflow/tool when the voice should be sent to a tied application.",
    }


def _resolve_provider(request: VoiceGenerateInput) -> str:
    normalized = request.provider.strip().lower()
    if normalized == "auto":
        if request.reference_voice_path and request.consent_confirmed:
            return "openvoice_local"
        return "system_tts"
    if normalized == "openvoice_local":
        return "openvoice_local"
    if normalized == "system_tts":
        return "system_tts"
    raise ToolExecutionError(f"Unsupported voice provider: {request.provider}")


def _system_tts_command() -> tuple[str, str] | None:
    if shutil.which("say"):
        return "say", ".aiff"
    for candidate in ("espeak-ng", "espeak"):
        if shutil.which(candidate):
            return candidate, ".wav"
    return None


def _system_tts_args(binary: str, output_path: Path, request: VoiceGenerateInput) -> list[str]:
    if binary == "say":
        args = [binary, "-o", str(output_path)]
        if request.voice:
            args.extend(["-v", request.voice])
        args.append(request.text)
        return args
    args = [binary, "-w", str(output_path)]
    if request.voice:
        args.extend(["-v", request.voice])
    args.append(request.text)
    return args


def _base_result(request: VoiceGenerateInput, *, provider: str, storage_key: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "text": request.text.strip(),
        "voice": request.voice,
        "reference_voice_path": request.reference_voice_path,
        "storage_key": storage_key,
        "storage_uri": None,
        "media_url": None,
        "file_path": None,
        "content_type": None,
        "ai_disclosure": request.ai_disclosure,
        "consent_confirmed": request.consent_confirmed,
        "purpose": request.purpose,
        "metadata": request.metadata,
    }


def _provider_warnings(requested_provider: str, resolved_provider: str) -> list[str]:
    warnings: list[str] = []
    if resolved_provider == "system_tts":
        warnings.append("system_tts uses the local host TTS engine and may vary by operating system.")
    if requested_provider == "auto" and resolved_provider == "openvoice_local":
        warnings.append("auto selected openvoice_local because a consented reference_voice_path was supplied.")
    return warnings


def _setup_instructions(provider: str) -> dict[str, Any]:
    if provider == "system_tts":
        return {
            "local_first": True,
            "install_options": ["macOS say is built in", "brew install espeak-ng", "apt-get install espeak-ng"],
        }
    if provider == "openvoice_local":
        return {
            "local_first": True,
            "required": [
                "Set AGENCY_OPENVOICE_ROOT to a local OpenVoice checkout, or install it at external/openvoice.",
                "Set AGENCY_OPENVOICE_CHECKPOINTS_DIR, or place V1 checkpoints under external/openvoice/checkpoints.",
                "Provide reference_voice_path and consent_confirmed=true.",
            ],
            "optional": [
                "Use AGENCY_OPENVOICE_TIMEOUT_SECONDS to adjust long local generation timeouts.",
            ],
        }
    return {"required": ["Local system TTS binary."], "local_first": True}


def _openvoice_readiness(request: VoiceGenerateInput) -> list[str]:
    warnings: list[str] = []
    reference_path = Path(str(request.reference_voice_path or "")).expanduser()
    if not reference_path.is_file():
        warnings.append(f"reference_voice_path does not exist: {reference_path}")
    if not OPENVOICE_ROOT.exists():
        warnings.append(f"OpenVoice root does not exist: {OPENVOICE_ROOT}")
    if not OPENVOICE_CHECKPOINTS_DIR.exists():
        warnings.append(f"OpenVoice checkpoints directory does not exist: {OPENVOICE_CHECKPOINTS_DIR}")
    return warnings


def _openvoice_args(request: VoiceGenerateInput, output_path: Path) -> list[str]:
    language = str(request.metadata.get("language") or "English")
    style = request.voice or str(request.metadata.get("style") or "default")
    return [
        _openvoice_python(),
        "-m",
        "app.tools.implementations.openvoice_runner",
        "--reference",
        str(Path(str(request.reference_voice_path)).expanduser()),
        "--text",
        request.text,
        "--output",
        str(output_path),
        "--checkpoints-dir",
        str(OPENVOICE_CHECKPOINTS_DIR),
        "--language",
        language,
        "--style",
        style,
    ]


def _openvoice_python() -> str:
    venv_python = OPENVOICE_ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return shutil.which("python3") or "python3"


def _openvoice_env() -> dict[str, str]:
    env = os.environ.copy()
    python_paths = [str(AGENCY_ROOT), str(OPENVOICE_ROOT)]
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    return env


def _storage_key_for(request: VoiceGenerateInput, *, suffix: str) -> str:
    output_name = _safe_output_name(request.output_name)
    filename = output_name or f"agency-voice-{uuid4().hex}{suffix}"
    if Path(filename).suffix == "":
        filename = f"{filename}{suffix}"
    prefix = request.storage_key_prefix.strip().strip("/") or "voice"
    return f"{prefix}/{filename}".replace("//", "/")


def _default_suffix_for_provider(provider: str) -> str:
    if provider == "system_tts" and shutil.which("say"):
        return ".aiff"
    return ".wav"


def _replace_suffix(storage_key: str, suffix: str) -> str:
    path = Path(storage_key)
    if path.suffix == suffix:
        return storage_key
    return str(path.with_suffix(suffix)).replace(os.sep, "/")


def _safe_output_name(output_name: str | None) -> str | None:
    if output_name is None:
        return None
    normalized = output_name.strip().replace("\\", "/")
    if not normalized:
        return None
    path = Path(normalized)
    if path.is_absolute() or any(part == ".." for part in normalized.split("/")):
        raise ValueError("output_name must be relative and cannot contain path traversal.")
    return normalized


__all__ = ["VoiceGenerateInput", "generate_voice"]
