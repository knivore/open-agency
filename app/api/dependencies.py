from __future__ import annotations

from fastapi import Request

from app.api.context import ApiContext, get_default_api_context


def get_api_context(request: Request | None = None) -> ApiContext:
    if request is not None:
        context = getattr(request.app.state, "api_context", None)
        if isinstance(context, ApiContext):
            return context
    return get_default_api_context()
