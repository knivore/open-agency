from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.api.schemas import PreSignedUrlRequest
from app.core.storage import generate_presigned_url


def create_storage_router() -> APIRouter:
    router = APIRouter(prefix="/storage", tags=["Storage"])

    @router.post("/presigned")
    async def create_presigned_url(request: PreSignedUrlRequest):
        try:
            url = generate_presigned_url(
                operation=request.operation,
                filename=request.filename,
                content_type=request.content_type,
            )
            return JSONResponse(content={"url": url})
        except HTTPException as exc:
            raise exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return router
