"""Agent catalog routes plus agent-specific execution lookup."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user, resolve_current_user_if_present
from app.core.config import get_settings
from app.domain import AgentDefinition
from app.services.agent_markdown_import import (
    AgentImportBatchCommitRequest,
    AgentImportBatchPreviewRequest,
    AgentImportCommitRequest,
    AgentImportError,
    AgentImportPreviewRequest,
    AgentMarkdownImportService,
    validation_error_detail,
)
from app.services.agents import AgentService
from ._crud import build_crud_router

MAX_AGENT_IMPORT_BYTES = 2 * 1024 * 1024


async def _read_agent_import(upload) -> bytes:
    raw = await upload.read(MAX_AGENT_IMPORT_BYTES + 1)
    if len(raw) > MAX_AGENT_IMPORT_BYTES:
        raise AgentImportError("Uploaded Markdown exceeds the 2 MiB size limit.", code="upload_too_large")
    return raw


async def _resolve_agent_user(request: Request, context: ApiContext, *, scopes: list[str]):
    if get_settings().app_env == "test":
        # Lightweight router fixtures historically omit identity; deployable
        # environments still enforce auth here even if the global middleware
        # is bypassed by an alternate application assembly.
        return await resolve_current_user_if_present(request, context, required_scopes=scopes)
    return await resolve_current_user(request, context, required_scopes=scopes)


def create_agents_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = AgentService(context)
    import_service = AgentMarkdownImportService(context)
    router = APIRouter(tags=["Agents"])

    @router.get("/agents/import/formats", summary="List Agent Markdown Import Formats")
    async def list_agent_import_formats(request: Request):
        await _resolve_agent_user(request, context, scopes=["agents:read"])
        return await import_service.formats()

    @router.post("/agents/import/preview", summary="Preview Agent Markdown Import")
    async def preview_agent_import(request: Request):
        current_user = await _resolve_agent_user(request, context, scopes=["agents:read"])
        try:
            payload = await _agent_import_preview_payload(request)
            proposal = await import_service.preview_from_request(payload, current_user=current_user)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=validation_error_detail(exc),
            ) from exc
        except AgentImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return proposal.model_dump(mode="json")

    @router.post("/agents/import/batch-preview", summary="Preview Batch Agent Markdown Import")
    async def preview_agent_import_batch(request: Request):
        current_user = await _resolve_agent_user(request, context, scopes=["agents:read"])
        try:
            payload = await _agent_import_batch_preview_payload(request)
            result = await import_service.batch_preview_from_request(payload, current_user=current_user)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=validation_error_detail(exc),
            ) from exc
        except AgentImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return result.model_dump(mode="json")

    @router.post("/agents/import/commit", summary="Commit Agent Markdown Import")
    async def commit_agent_import(request: Request):
        current_user = await _resolve_agent_user(request, context, scopes=["agents:write"])
        try:
            payload = AgentImportCommitRequest.model_validate(await request.json())
            result = await import_service.commit_from_request(payload, current_user=current_user)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=validation_error_detail(exc),
            ) from exc
        except AgentImportError as exc:
            status_code = (
                status.HTTP_409_CONFLICT
                if exc.code == "agent_import_conflict"
                else status.HTTP_422_UNPROCESSABLE_CONTENT
            )
            raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": str(exc)}) from exc
        return result.model_dump(mode="json")

    @router.post("/agents/import/batch-commit", summary="Commit Batch Agent Markdown Import")
    async def commit_agent_import_batch(request: Request):
        current_user = await _resolve_agent_user(request, context, scopes=["agents:write"])
        try:
            payload = AgentImportBatchCommitRequest.model_validate(await request.json())
            result = await import_service.batch_commit_from_request(payload, current_user=current_user)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=validation_error_detail(exc),
            ) from exc
        except AgentImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return result.model_dump(mode="json")

    crud_router = build_crud_router(
        prefix="/agents",
        tag="Agents",
        summary_name="Agent",
        repo=context.agent_repo,
        model_cls=AgentDefinition,
        context=context,
        read_scopes=["agents:read"],
        write_scopes=["agents:write"],
        require_read_auth=get_settings().app_env != "test",
        require_write_auth=get_settings().app_env != "test",
    )
    router.include_router(crud_router)

    @router.get("/agents/{agent_id}/executions", summary="List Executions For Agent")
    async def list_agent_executions(agent_id: str, request: Request):
        await resolve_current_user(request, context, required_scopes=["executions:read"])
        return await service.list_agent_executions(agent_id)

    return router


async def _agent_import_preview_payload(request: Request) -> AgentImportPreviewRequest:
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        markdown_text = form.get("markdown_text")
        if hasattr(upload, "read") and hasattr(upload, "filename"):
            raw = await _read_agent_import(upload)
            try:
                markdown_text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AgentImportError("Uploaded Markdown file must be UTF-8 text.",
                                       code="upload_encoding_invalid") from exc
            source_filename = upload.filename
            if hasattr(upload, "close"):
                await upload.close()
        else:
            source_filename = _form_string(form.get("source_filename"))
        return AgentImportPreviewRequest(
            markdown_text=_form_string(markdown_text),
            source_url=_form_string(form.get("source_url")),
            source_filename=source_filename,
            use_llm_normalization=_form_bool(form.get("use_llm_normalization")),
            llm_normalization_model_profile_id=_form_string(form.get("llm_normalization_model_profile_id")),
        )
    return AgentImportPreviewRequest.model_validate(await request.json())


async def _agent_import_batch_preview_payload(request: Request) -> AgentImportBatchPreviewRequest:
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        items: list[AgentImportPreviewRequest] = []
        for _, value in form.multi_items():
            if not (hasattr(value, "read") and hasattr(value, "filename")):
                continue
            raw = await _read_agent_import(value)
            try:
                markdown_text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                if hasattr(value, "close"):
                    await value.close()
                raise AgentImportError(
                    f"Uploaded Markdown file '{value.filename}' must be UTF-8 text.",
                    code="upload_encoding_invalid",
                ) from exc
            items.append(
                AgentImportPreviewRequest(
                    markdown_text=markdown_text,
                    source_filename=value.filename,
                    use_llm_normalization=_form_bool(form.get("use_llm_normalization")),
                    llm_normalization_model_profile_id=_form_string(form.get("llm_normalization_model_profile_id")),
                )
            )
            if hasattr(value, "close"):
                await value.close()
        if not items:
            raise AgentImportError("Upload at least one Markdown file.", code="missing_source")
        return AgentImportBatchPreviewRequest(items=items)
    return AgentImportBatchPreviewRequest.model_validate(await request.json())


def _form_string(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value).strip() or None


def _form_bool(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
