"""Health, root metadata, setup readiness, and storage compatibility routes."""

from __future__ import annotations

import httpx
import os
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user, resolve_current_user_if_present
from app.api.schemas.storage import PreSignedUrlRequest
from app.core.config import get_settings
from app.core.storage import generate_presigned_url, get_local_file_path, mock_upload_to_local
from app.db.session import is_database_configured, ping_database
from app.services.main_agent_setup.service import MainAgentSetupService

MAX_LOCAL_STORAGE_UPLOAD_BYTES = 10 * 1024 * 1024


async def _require_integrations_user(request: Request, context: ApiContext, *, scopes: list[str]):
    if get_settings().app_env == "test":
        return await resolve_current_user_if_present(request, context, required_scopes=scopes)
    return await resolve_current_user(request, context, required_scopes=scopes)


async def _read_limited_body(request: Request, *, limit: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > limit:
                raise HTTPException(status_code=413, detail="Upload exceeds the 10 MiB size limit")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length must be an integer") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="Upload exceeds the 10 MiB size limit")
    return bytes(body)


async def _database_status(context: ApiContext) -> dict[str, object]:
    # In-memory test contexts own persistence directly and should not fail setup
    # readiness because they are not backed by a real async database connection.
    if not context.database_health_checks_enabled:
        return {
            "configured": True,
            "reachable": True,
            "detail": None,
        }
    if not is_database_configured():
        return {
            "configured": False,
            "reachable": False,
            "detail": "DATABASE_URL is not configured",
        }
    try:
        await ping_database()
    except Exception as exc:
        return {
            "configured": True,
            "reachable": False,
            "detail": str(exc),
        }
    return {
        "configured": True,
        "reachable": True,
        "detail": None,
    }


def create_health_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
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
        status = await _database_status(context)
        if not status["reachable"]:
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "configured": status["configured"],
                    "detail": status["detail"],
                },
            )
        return {"ok": True, "configured": True}

    @router.get("/setup/status")
    async def setup_status():
        from app.services.openvoice_setup import OpenVoiceSetupService

        settings = get_settings()
        database = await _database_status(context)
        users = await context.user_repo.list()
        has_admin = any("admin" in user.roles for user in users)
        setup_service = MainAgentSetupService(context)
        has_model_profiles = await setup_service.has_usable_model_profiles()
        main_agent_complete = await setup_service.is_main_agent_setup_complete()
        bootstrap_configured = setup_service.bootstrap_config_from_settings(settings) is not None

        blockers: list[str] = []
        if not database["configured"]:
            blockers.append("database_not_configured")
        elif not database["reachable"]:
            blockers.append("database_unreachable")
        if not users:
            blockers.append("no_users")
        if not has_admin:
            blockers.append("no_admin_user")
        if not has_model_profiles:
            blockers.append("no_model_profiles")
        if not main_agent_complete:
            blockers.append("main_agent_not_configured")

        ready = not blockers
        next_path = "/workflows" if ready else "/setup"
        openvoice = OpenVoiceSetupService().status()
        return {
            "ready": ready,
            "next_path": next_path,
            "blockers": blockers,
            "database": database,
            "users": {
                "count": len(users),
                "has_admin": has_admin,
                "auth_bootstrap_supported": not has_admin,
                "recommended_bootstrap": "local_admin_setup" if not has_admin else "signin_and_continue_setup",
            },
            "models": {
                "has_usable_model_profiles": has_model_profiles,
                "bootstrap_configured": bootstrap_configured,
            },
            "main_agent": {
                "configured": main_agent_complete,
            },
            # The public setup summary intentionally omits local filesystem paths;
            # authenticated Profile APIs expose the detailed diagnostic status.
            "openvoice": {
                "optional": True,
                "ready": openvoice["ready"],
                "supports_cloning": openvoice["supports_cloning"],
                "runtime_installed": openvoice["runtime"]["installed"],
                "checkpoints_installed": openvoice["checkpoints"]["installed"],
                "default_voice": openvoice["settings"]["default_voice"],
            },
        }

    @router.get("/health/onecli")
    async def health_onecli():
        settings = get_settings()
        diagnostics = settings.sanitized_onecli_diagnostics
        if not settings.onecli_enabled:
            return {"ok": True, "configured": False, **diagnostics}

        async def check_url(url: str) -> dict[str, object]:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(url)
                return {"reachable": response.status_code < 500, "status_code": response.status_code}
            except httpx.HTTPError as exc:
                return {"reachable": False, "error": exc.__class__.__name__}

        api = await check_url(settings.onecli_api_url)
        gateway = await check_url(settings.onecli_gateway_url)
        ok = bool(api.get("reachable")) and bool(gateway.get("reachable"))
        status_code = 200 if ok else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "ok": ok,
                "configured": True,
                **diagnostics,
                "api": api,
                "gateway": gateway,
            },
        )

    @router.post("/api/presigned")
    async def generate_presigned_url_(payload: PreSignedUrlRequest, request: Request):
        try:
            await _require_integrations_user(request, context, scopes=["integrations:write"])
            url = generate_presigned_url(
                operation=payload.operation,
                filename=payload.filename,
                content_type=payload.content_type,
            )
            return JSONResponse(content={"url": url})
        except HTTPException as exc:
            raise exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Unable to generate a presigned URL") from exc

    @router.put("/api/local-storage/upload")
    async def upload_file(request: Request, file: str = Query(...)):
        try:
            await _require_integrations_user(request, context, scopes=["integrations:write"])
            body = await _read_limited_body(request, limit=MAX_LOCAL_STORAGE_UPLOAD_BYTES)
            if not file:
                raise HTTPException(status_code=400, detail="Filename not provided")
            target_path = get_local_file_path(file)
            mock_upload_to_local(body, target_path)
            return JSONResponse(
                content={"message": "File uploaded successfully", "filename": file, "path": file},
                status_code=200,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException as exc:
            raise exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Unable to upload the file") from exc

    @router.get("/api/local-storage/download")
    async def download_file(request: Request, file: str = Query(...)):
        try:
            await _require_integrations_user(request, context, scopes=["integrations:read"])
            path_to_use = get_local_file_path(file)
            if not path_to_use or not path_to_use.strip():
                raise HTTPException(status_code=400, detail="Filename not provided")
            if not os.path.exists(path_to_use):
                raise HTTPException(status_code=404, detail=f"File not found: {file}")
            return FileResponse(
                path=path_to_use,
                filename=os.path.basename(path_to_use),
                media_type="application/octet-stream",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except HTTPException as exc:
            raise exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Unable to download the file") from exc

    return router
