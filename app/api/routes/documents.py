"""Document upload endpoint that extracts content into durable memory."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.domain import MemoryScope
from app.services.document_ingestion import (
    DocumentIngestionError,
    DocumentIngestionService,
    DocumentUploadIntelligenceService,
    MAX_DOCUMENT_UPLOAD_BYTES,
)
from app.services.memory import MemoryPermissionError

DOCUMENT_MEMORY_SCOPES = {
    MemoryScope.USER.value,
    MemoryScope.WORKSPACE.value,
    MemoryScope.CONVERSATION.value,
    MemoryScope.WORKFLOW.value,
}


def _parse_tags(raw_tags: str | None) -> list[str]:
    if not raw_tags:
        return []
    return [item.strip() for item in raw_tags.split(",") if item.strip()]


def create_documents_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    router = APIRouter(prefix="/documents", tags=["Documents"])

    @router.post("/ingest", summary="Ingest Document Into Memory")
    async def ingest_document(
            request: Request,
            file: UploadFile = File(...),
            scope: str = Form(default=MemoryScope.USER.value),
            workspace_id: str | None = Form(default=None),
            conversation_id: str | None = Form(default=None),
            workflow_id: str | None = Form(default=None),
            agent_id: str | None = Form(default=None),
            tags: str | None = Form(default=None),
            chunk_size: int | None = Form(default=None),
            chunk_overlap: int | None = Form(default=None),
            auto_intelligence: bool = Form(default=False),
            allow_scope_suggestion: bool = Form(default=False),
            allow_agent_suggestion: bool = Form(default=False),
            purpose: str = Form(default="memory"),
            upload_mode: str = Form(default="vector"),
    ):
        if scope not in DOCUMENT_MEMORY_SCOPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Invalid document memory scope. Choose user, workspace, conversation, "
                    "or workflow."
                ),
            )
        current_user = await resolve_current_user(request, context, required_scopes=["memory:write"])
        try:
            result = await DocumentIngestionService(context).ingest_upload(
                upload=file,
                current_user=current_user,
                scope=scope,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                tags=_parse_tags(tags),
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                auto_intelligence=auto_intelligence,
                allow_scope_suggestion=allow_scope_suggestion,
                allow_agent_suggestion=allow_agent_suggestion,
                purpose=purpose,
                upload_mode=upload_mode,
            )
        except DocumentIngestionError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return {
            "document_id": result.document_id,
            "filename": result.filename,
            "content_type": result.content_type,
            "storage_uri": result.storage_uri,
            "text_characters": result.text_characters,
            "estimated_tokens": result.estimated_tokens,
            "upload_mode": result.upload_mode,
            "context_attachment_id": result.context_attachment_id,
            "chunks_created": result.chunks_created,
            "memory_ids": result.memory_ids,
        }

    @router.get("", summary="List Uploaded Documents")
    async def list_uploaded_documents(
            request: Request,
            conversation_id: str | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
            scope: str | None = None,
            upload_mode: str | None = None,
            limit: int = 50,
    ):
        current_user = await resolve_current_user(request, context, required_scopes=["memory:read"])
        repo = getattr(context, "uploaded_document_repo", None)
        if repo is None or not hasattr(repo, "query"):
            return {"items": []}
        items = await repo.query(
            conversation_id=conversation_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
            user_id=current_user.id,
            scope=scope,
            upload_mode=upload_mode,
            limit=max(min(limit, 100), 0),
        )
        return {"items": [_uploaded_document_payload(item) for item in items]}

    @router.get("/{document_id}", summary="Get Uploaded Document Metadata")
    async def get_uploaded_document(request: Request, document_id: str):
        current_user = await resolve_current_user(request, context, required_scopes=["memory:read"])
        repo = getattr(context, "uploaded_document_repo", None)
        if repo is None or not hasattr(repo, "get"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Uploaded document not found.")
        item = await repo.get(document_id)
        if item is None or item.created_by_user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Uploaded document not found.")
        return _uploaded_document_payload(item)

    @router.delete("/{document_id}", summary="Delete Uploaded Document")
    async def delete_uploaded_document(request: Request, document_id: str):
        current_user = await resolve_current_user(request, context, required_scopes=["memory:write"])
        try:
            result = await DocumentIngestionService(context).delete_uploaded_document(
                document_id,
                current_user=current_user,
            )
        except MemoryPermissionError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Uploaded document not found.")
        return {
            "deleted": True,
            "document_id": result.document_id,
            "upload_mode": result.upload_mode,
            "document_status": result.document_status,
            "memory_ids": result.deleted_memory_ids,
            "deleted_memory_count": len(result.deleted_memory_ids),
        }

    @router.post("/intelligence", summary="Recommend Document Upload Settings")
    async def recommend_document_upload_settings(
            request: Request,
            file: UploadFile = File(...),
            scope: str = Form(default=MemoryScope.USER.value),
            workspace_id: str | None = Form(default=None),
            conversation_id: str | None = Form(default=None),
            workflow_id: str | None = Form(default=None),
            agent_id: str | None = Form(default=None),
            tags: str | None = Form(default=None),
            chunk_size: int | None = Form(default=None),
            chunk_overlap: int | None = Form(default=None),
            purpose: str = Form(default="memory"),
    ):
        if scope not in DOCUMENT_MEMORY_SCOPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Invalid document memory scope. Choose user, workspace, conversation, "
                    "or workflow."
                ),
            )
        current_user = await resolve_current_user(request, context, required_scopes=["memory:write"])
        try:
            filename = DocumentIngestionService._safe_filename(file.filename or "document")
            raw = await file.read(MAX_DOCUMENT_UPLOAD_BYTES + 1)
            if len(raw) > MAX_DOCUMENT_UPLOAD_BYTES:
                raise DocumentIngestionError("Uploaded document exceeds the 10 MiB size limit.")
            if not raw:
                raise DocumentIngestionError("Uploaded document is empty.")
            text = DocumentIngestionService.extract_text(raw, filename=filename, content_type=file.content_type)
            if not text.strip():
                raise DocumentIngestionError("No extractable text was found in the uploaded document.")
            recommendation = await DocumentUploadIntelligenceService(context).recommend_upload(
                filename=filename,
                content_type=file.content_type,
                text=text,
                current_user=current_user,
                purpose=purpose,
                current={
                    "scope": scope,
                    "workspace_id": workspace_id,
                    "conversation_id": conversation_id,
                    "workflow_id": workflow_id,
                    "agent_id": agent_id,
                    "tags": _parse_tags(tags),
                    "chunk_size": chunk_size,
                    "chunk_overlap": chunk_overlap,
                },
            )
        except DocumentIngestionError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return recommendation.as_payload()

    return router


__all__ = ["create_documents_router"]


def _uploaded_document_payload(item) -> dict:
    return {
        "id": item.id,
        "filename": item.filename,
        "content_type": item.content_type,
        "storage_uri": item.storage_uri,
        "text_characters": item.text_characters,
        "estimated_tokens": item.estimated_tokens,
        "upload_mode": item.upload_mode.value if hasattr(item.upload_mode, "value") else item.upload_mode,
        "scope": item.scope,
        "created_by_user_id": item.created_by_user_id,
        "workspace_id": item.workspace_id,
        "conversation_id": item.conversation_id,
        "workflow_id": item.workflow_id,
        "agent_id": item.agent_id,
        "status": item.status.value if hasattr(item.status, "value") else item.status,
        "metadata": item.metadata,
        "created_at": item.created_at.isoformat() if hasattr(item.created_at, "isoformat") else item.created_at,
        "updated_at": item.updated_at.isoformat() if hasattr(item.updated_at, "isoformat") else item.updated_at,
    }
