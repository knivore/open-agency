from __future__ import annotations

from typing import Any, Literal, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.core.config import get_settings
from app.services.audio import OpenAIAudioConfigurationError, OpenAIRealtimeTranscriptionService


class RealtimeTranscriptionSessionRequest(BaseModel):
    model: str | None = Field(
        default=None,
        description="OpenAI transcription model. Defaults to OPENAI_REALTIME_TRANSCRIPTION_MODEL or whisper-1.",
    )
    input_audio_format: Literal["pcm16", "g711_ulaw", "g711_alaw"] = "pcm16"
    language: str | None = None
    prompt: str | None = None
    turn_detection: dict[str, Any] | None = Field(
        default_factory=lambda: {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 500,
        }
    )
    input_audio_noise_reduction: dict[str, Any] | None = None
    include: list[str] | None = None


def create_voice_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/voice", tags=["Voice"])

    @router.post("/realtime/transcription-session", summary="Create Realtime Transcription Session")
    async def create_realtime_transcription_session(
            payload: RealtimeTranscriptionSessionRequest,
            request: Request,
    ):
        await resolve_current_user(request, context, required_scopes=["conversations:write"])
        settings = get_settings()
        model = payload.model or settings.openai_realtime_transcription_model
        service = OpenAIRealtimeTranscriptionService(
            api_key=settings.openai_api_key,
            base_url=settings.openai_api_base_url,
            timeout=settings.llm_request_timeout_seconds,
        )
        try:
            return await service.create_session(
                model=model,
                input_audio_format=payload.input_audio_format,
                language=payload.language,
                prompt=payload.prompt,
                turn_detection=payload.turn_detection,
                input_audio_noise_reduction=payload.input_audio_noise_reduction,
                include=payload.include,
            )
        except OpenAIAudioConfigurationError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            detail = _openai_error_detail(exc.response)
            raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return router


def _openai_error_detail(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    return payload.get("error") or payload
