from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.domain import MemoryScope
from app.services.document_ingestion import DocumentIngestionError, DocumentIngestionService


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
            chunk_size: int = Form(default=1200),
            chunk_overlap: int = Form(default=150),
    ):
        if scope not in {item.value for item in MemoryScope}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid memory scope.")
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
            )
        except DocumentIngestionError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return {
            "document_id": result.document_id,
            "filename": result.filename,
            "content_type": result.content_type,
            "storage_uri": result.storage_uri,
            "text_characters": result.text_characters,
            "chunks_created": result.chunks_created,
            "memory_ids": result.memory_ids,
        }

    return router


__all__ = ["create_documents_router"]
