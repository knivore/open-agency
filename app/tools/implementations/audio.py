from __future__ import annotations

import base64
import binascii
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from openai import OpenAI
from pydantic import BaseModel, Field, model_validator

from app.runtime.native.errors import ToolExecutionError


class TranscribeAudioInput(BaseModel):
    file_path: str | None = Field(
        default=None,
        description="Path to a local audio file accessible to the backend.",
    )
    audio_base64: str | None = Field(
        default=None,
        description="Base64 encoded audio bytes. Data URLs are accepted.",
    )
    filename: str = Field(
        default="audio.webm",
        description="Filename to use when audio_base64 is provided.",
    )
    model: str = Field(
        default="whisper-1",
        description="OpenAI speech-to-text model, for example whisper-1 or gpt-4o-mini-transcribe.",
    )
    language: str | None = Field(
        default=None,
        description="Optional ISO-639-1 language hint such as en.",
    )
    prompt: str | None = Field(
        default=None,
        description="Optional prompt or keyword list to guide transcription.",
    )
    response_format: Literal["json", "text", "verbose_json"] = Field(
        default="json",
        description="Transcription response format.",
    )

    @model_validator(mode="after")
    def require_exactly_one_audio_source(self) -> "TranscribeAudioInput":
        provided = [bool(self.file_path), bool(self.audio_base64)]
        if sum(provided) != 1:
            raise ValueError("Provide exactly one of file_path or audio_base64")
        return self


def transcribe_audio(
        *,
        file_path: str | None = None,
        audio_base64: str | None = None,
        filename: str = "audio.webm",
        model: str | None = None,
        language: str | None = None,
        prompt: str | None = None,
        response_format: Literal["json", "text", "verbose_json"] = "json",
) -> dict[str, Any]:
    request = TranscribeAudioInput.model_validate(
        {
            "file_path": file_path,
            "audio_base64": audio_base64,
            "filename": filename,
            "model": model or os.getenv("OPENAI_AUDIO_TRANSCRIPTION_MODEL") or "whisper-1",
            "language": language,
            "prompt": prompt,
            "response_format": response_format,
        }
    )
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ToolExecutionError("OPENAI_API_KEY is required to transcribe audio")

    temp_path: Path | None = None
    try:
        source_path = Path(request.file_path) if request.file_path else _write_base64_audio(
            request.audio_base64 or "",
            request.filename,
        )
        if not source_path.exists() or not source_path.is_file():
            raise ToolExecutionError(f"Audio file was not found: {source_path}")
        if source_path != Path(request.file_path or ""):
            temp_path = source_path

        kwargs: dict[str, Any] = {
            "model": request.model,
            "response_format": request.response_format,
        }
        if request.language:
            kwargs["language"] = request.language
        if request.prompt:
            kwargs["prompt"] = request.prompt

        client = OpenAI(api_key=api_key)
        with source_path.open("rb") as audio_file:
            result = client.audio.transcriptions.create(file=audio_file, **kwargs)

        return _normalize_transcription_result(
            result,
            model=request.model,
            response_format=request.response_format,
            language=request.language,
        )
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _write_base64_audio(audio_base64: str, filename: str) -> Path:
    payload = audio_base64.strip()
    if "," in payload and payload.lower().startswith("data:"):
        _, payload = payload.split(",", 1)
    try:
        audio_bytes = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolExecutionError("audio_base64 must contain valid base64 audio data") from exc
    suffix = Path(filename).suffix or ".webm"
    with tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False) as handle:
        handle.write(audio_bytes)
        return Path(handle.name)


def _normalize_transcription_result(
        result: Any,
        *,
        model: str,
        response_format: str,
        language: str | None,
) -> dict[str, Any]:
    if isinstance(result, str):
        return {
            "status": "success",
            "text": result,
            "model": model,
            "language": language,
            "response_format": response_format,
        }

    if hasattr(result, "model_dump"):
        raw = result.model_dump(mode="json")
    elif isinstance(result, dict):
        raw = dict(result)
    else:
        raw = {"text": str(result)}

    return {
        "status": "success",
        "text": raw.get("text", ""),
        "model": model,
        "language": language or raw.get("language"),
        "duration": raw.get("duration"),
        "segments": raw.get("segments"),
        "response_format": response_format,
        "raw_response": raw,
    }
