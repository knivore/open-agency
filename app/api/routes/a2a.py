from __future__ import annotations

from fastapi import APIRouter
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.protocols.a2a.routes import create_a2a_router as create_protocol_a2a_router


def create_a2a_router(context: Optional[ApiContext] = None) -> APIRouter:
    return create_protocol_a2a_router(context or get_default_api_context())
