"""Generic Agency speech announcement capability service."""

from __future__ import annotations

from app.domain.speech import SpeechAnnouncementRequest, SpeechAnnouncementResponse


class SpeechAnnouncementService:
    async def announce(self, request: SpeechAnnouncementRequest) -> SpeechAnnouncementResponse:
        normalized_text = request.text.strip()
        return SpeechAnnouncementResponse(
            text=normalized_text,
            targetKind=request.targetKind,
            targetRef=request.targetRef,
            channel=request.channel,
            ssml=request.ssml,
            voice=request.voice,
            metadata=request.metadata,
        )
