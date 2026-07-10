"""Domain contracts for generic Agency speech capabilities."""

from __future__ import annotations

from pydantic import Field
from typing import Any, Literal
from uuid import uuid4

from .credentials import DomainModel

SpeechAnnouncementTargetKind = Literal["home", "room", "speaker", "surface"]


class SpeechAnnouncementRequest(DomainModel):
    text: str
    targetKind: SpeechAnnouncementTargetKind | None = None
    targetRef: str | None = None
    channel: str | None = None
    ssml: str | None = None
    voice: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SpeechAnnouncementResponse(DomainModel):
    announcementId: str = Field(default_factory=lambda: f"announce_{uuid4().hex}")
    status: Literal["accepted"] = "accepted"
    text: str
    targetKind: SpeechAnnouncementTargetKind | None = None
    targetRef: str | None = None
    channel: str | None = None
    ssml: str | None = None
    voice: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SpeechContinuationRequest(DomainModel):
    responseText: str
    surface: str = "speech"
    sessionId: str | None = None
    priorAnnouncementId: str | None = None
    channel: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SpeechContinuationResponse(DomainModel):
    continuationId: str = Field(default_factory=lambda: f"continue_{uuid4().hex}")
    status: Literal["completed"] = "completed"
    replyText: str
    replySsml: str | None = None
    actionsTaken: list[dict[str, Any]] = Field(default_factory=list)
    sessionId: str | None = None
    priorAnnouncementId: str | None = None
    channel: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "SpeechAnnouncementRequest",
    "SpeechAnnouncementResponse",
    "SpeechAnnouncementTargetKind",
    "SpeechContinuationRequest",
    "SpeechContinuationResponse",
]
