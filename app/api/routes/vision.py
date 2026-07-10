"""Ambient vision routes."""

from __future__ import annotations

import base64
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user_if_present
from app.vision.vision_client import VisionClient


class AnalyseSnapshotRequest(BaseModel):
    image_base64: str = Field(alias="imageBase64")
    media_type: str = Field(default="image/jpeg", alias="mediaType")
    question: str | None = None


def create_vision_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/api/vision", tags=["Ambient Vision"])

    @router.post("/analyse-snapshot")
    async def analyse_snapshot(payload: AnalyseSnapshotRequest, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["integrations:read"])
        try:
            image_bytes = base64.b64decode(payload.image_base64)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="imageBase64 must be valid base64") from exc
        result = VisionClient().analyse_image(image_bytes, media_type=payload.media_type, question=payload.question)
        return {"analysis": result}

    return router
