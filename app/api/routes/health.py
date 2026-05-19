from __future__ import annotations

import os
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api.schemas import PreSignedUrlRequest
from app.core.storage import generate_presigned_url, get_local_file_path, mock_upload_to_local
from app.db.session import is_database_configured, ping_database


def create_health_router() -> APIRouter:
    router = APIRouter(tags=["Health"])

    @router.get("/")
    async def read_root():
        return {
            "message": "Welcome to Agency",
            "docs": "/docs",
            "openapi": "/openapi.json",
            "redoc": "/redoc",
        }

    @router.get("/health")
    async def health():
        return {"ok": True}

    @router.get("/health/db")
    async def health_db():
        if not is_database_configured():
            return JSONResponse(
                status_code=503,
                content={"ok": False, "configured": False, "detail": "DATABASE_URL is not configured"},
            )
        try:
            await ping_database()
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "configured": True, "detail": str(exc)},
            )
        return {"ok": True, "configured": True}

    @router.post("/api/presigned")
    async def generate_presigned_url_(request: PreSignedUrlRequest):
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

    @router.put("/api/local-storage/upload")
    async def upload_file(request: Request, file: str = Query(...)):
        try:
            body = await request.body()
            if not file:
                raise HTTPException(status_code=400, detail="Filename not provided")
            target_path = get_local_file_path(file)
            mock_upload_to_local(body, target_path)
            return JSONResponse(
                content={"message": "File uploaded successfully", "filename": file, "path": file},
                status_code=200,
            )
        except HTTPException as exc:
            raise exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Error uploading file: {str(exc)}") from exc

    @router.get("/api/local-storage/download")
    async def download_file(file: str = Query(...)):
        try:
            file_path = file
            relative_path = get_local_file_path(file)
            if os.path.exists(file_path):
                path_to_use = file_path
            elif os.path.exists(relative_path):
                path_to_use = relative_path
            else:
                raise HTTPException(status_code=404, detail=f"File not found: {file}")
            return FileResponse(
                path=path_to_use,
                filename=os.path.basename(path_to_use),
                media_type="application/octet-stream",
            )
        except HTTPException as exc:
            raise exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Error downloading file: {str(exc)}") from exc

    return router
