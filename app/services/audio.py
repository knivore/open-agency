from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse
from typing import Any

import httpx


class OpenAIAudioConfigurationError(RuntimeError):
    pass


class OpenAIRealtimeTranscriptionService:
    def __init__(
            self,
            *,
            api_key: str | None = None,
            base_url: str | None = None,
            timeout: float = 15.0,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = (base_url or os.getenv("OPENAI_API_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.timeout = timeout

    async def create_session(
            self,
            *,
            model: str = "whisper-1",
            input_audio_format: str = "pcm16",
            language: str | None = None,
            prompt: str | None = None,
            turn_detection: dict[str, Any] | None = None,
            input_audio_noise_reduction: dict[str, Any] | None = None,
            include: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise OpenAIAudioConfigurationError("OPENAI_API_KEY is required to create realtime transcription sessions")

        transcription: dict[str, Any] = {"model": model}
        if language:
            transcription["language"] = language
        if prompt:
            transcription["prompt"] = prompt

        payload: dict[str, Any] = {
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": _realtime_audio_format(input_audio_format),
                        "transcription": transcription,
                    }
                },
            }
        }
        session = payload["session"]
        audio_input = session["audio"]["input"]
        if turn_detection is not None:
            audio_input["turn_detection"] = turn_detection
        if input_audio_noise_reduction is not None:
            audio_input["noise_reduction"] = input_audio_noise_reduction
        if include is not None:
            session["include"] = include

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/realtime/client_secrets",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        response.raise_for_status()
        data = response.json()
        return {
            **data,
            "connection": {
                "websocket_url": _realtime_websocket_url(self.base_url),
                "mode": "transcription",
            },
        }


def _realtime_audio_format(input_audio_format: str) -> dict[str, Any]:
    mapping = {
        "pcm16": {"type": "audio/pcm", "rate": 24000},
        "audio/pcm": {"type": "audio/pcm", "rate": 24000},
        "g711_ulaw": {"type": "audio/pcmu"},
        "audio/pcmu": {"type": "audio/pcmu"},
        "g711_alaw": {"type": "audio/pcma"},
        "audio/pcma": {"type": "audio/pcma"},
    }
    return mapping.get(input_audio_format, {"type": input_audio_format})


def _realtime_websocket_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = f"{path}/realtime"
    else:
        path = f"{path}/v1/realtime" if path else "/v1/realtime"
    return urlunparse((scheme, parsed.netloc, path, "", "intent=transcription", ""))
