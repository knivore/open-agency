from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any

from app.domain.speech import SpeechAnnouncementRequest, SpeechContinuationRequest
from app.services.speech_announcements import SpeechAnnouncementService
from app.services.speech_continuations import SpeechContinuationService
from app.tools.implementations.audio import TranscribeAudioInput, transcribe_audio


class SpeechListenInput(TranscribeAudioInput):
    """Agency speech-listening contract backed by the shared audio transcription flow."""


class SpeechSpeakInput(BaseModel):
    text: str = Field(description="Announcement text to speak or deliver on the selected speech surface.")
    targetKind: str | None = Field(default=None,
                                   description="Optional target scope such as home, room, speaker, or surface.")
    targetRef: str | None = Field(
        default=None,
        description="Optional entity, room, or surface reference understood by the destination adapter.",
    )
    channel: str | None = Field(default=None, description="Optional delivery channel label such as voice or phone.")
    ssml: str | None = Field(
        default=None,
        description="Optional SSML payload when the downstream speech adapter supports it.",
    )
    voice: str | None = Field(default=None, description="Optional voice preset or provider-specific voice id.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional delivery metadata for downstream channel selection or audit context.",
    )


class SpeechContinueInput(BaseModel):
    responseText: str = Field(description="The follow-up spoken response that should continue the conversation.")
    surface: str = Field(default="speech",
                         description="Surface where the follow-up was captured, such as speaker or phone.")
    sessionId: str | None = Field(
        default=None,
        description="Optional session identifier used to continue an existing speech conversation.",
    )
    priorAnnouncementId: str | None = Field(
        default=None,
        description="Optional announcement id that prompted the follow-up response.",
    )
    channel: str | None = Field(default=None, description="Optional channel label such as voice or phone.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional continuation metadata forwarded to downstream speech handlers.",
    )


def listen_speech(**kwargs: Any) -> dict[str, Any]:
    return transcribe_audio(**kwargs)


async def speak_speech(
        *,
        text: str,
        targetKind: str | None = None,
        targetRef: str | None = None,
        channel: str | None = None,
        ssml: str | None = None,
        voice: str | None = None,
        metadata: dict[str, Any] | None = None,
        tool_context=None,  # noqa: ANN001
) -> dict[str, Any]:
    request = SpeechAnnouncementRequest(
        text=text,
        targetKind=targetKind,
        targetRef=targetRef,
        channel=channel,
        ssml=ssml,
        voice=voice,
        metadata=metadata or {},
    )
    response = await SpeechAnnouncementService().announce(request)
    return response.model_dump(mode="json")


async def continue_speech(
        *,
        responseText: str,
        surface: str = "speech",
        sessionId: str | None = None,
        priorAnnouncementId: str | None = None,
        channel: str | None = None,
        metadata: dict[str, Any] | None = None,
        tool_context=None,  # noqa: ANN001
) -> dict[str, Any]:
    request = SpeechContinuationRequest(
        responseText=responseText,
        surface=surface,
        sessionId=sessionId,
        priorAnnouncementId=priorAnnouncementId,
        channel=channel,
        metadata=metadata or {},
    )
    response = await SpeechContinuationService().continue_response(request)
    return response.model_dump(mode="json")


__all__ = ["SpeechListenInput", "SpeechSpeakInput", "SpeechContinueInput", "listen_speech", "speak_speech",
           "continue_speech"]
