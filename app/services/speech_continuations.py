"""Generic Agency speech continuation service."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.speech import SpeechContinuationRequest, SpeechContinuationResponse


@dataclass(slots=True)
class SpeechContinuationService:
    async def continue_response(self, request: SpeechContinuationRequest) -> SpeechContinuationResponse:
        return SpeechContinuationResponse(
            replyText=request.responseText,
            replySsml=None,
            actionsTaken=[],
            sessionId=request.sessionId,
            priorAnnouncementId=request.priorAnnouncementId,
            channel=request.channel,
            metadata=request.metadata,
        )
