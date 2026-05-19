from __future__ import annotations

import hashlib
import io
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import docx2txt
import pdfplumber
from fastapi import UploadFile

from app.core.storage import upload_to_s3
from app.domain import MemoryKind, MemoryScope, UserDefinition
from app.services.memory import MemoryService


SUPPORTED_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".html", ".htm"}
SUPPORTED_DOCUMENT_EXTENSIONS = {*SUPPORTED_TEXT_EXTENSIONS, ".pdf", ".docx"}


class DocumentIngestionError(ValueError):
    pass


@dataclass(slots=True)
class DocumentChunk:
    index: int
    text: str
    start_char: int
    end_char: int


@dataclass(slots=True)
class DocumentIngestionResult:
    document_id: str
    filename: str
    content_type: str | None
    storage_uri: str | None
    text_characters: int
    chunks_created: int
    memory_ids: list[str]


class DocumentIngestionService:
    def __init__(self, context):
        self.context = context
        self.memory_service = MemoryService(context)

    async def ingest_upload(
            self,
            *,
            upload: UploadFile,
            current_user: UserDefinition,
            scope: str = MemoryScope.USER.value,
            workspace_id: str | None = None,
            conversation_id: str | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
            source: str = "document_upload",
            tags: list[str] | None = None,
            chunk_size: int = 1200,
            chunk_overlap: int = 150,
    ) -> DocumentIngestionResult:
        filename = self._safe_filename(upload.filename or "document")
        extension = Path(filename).suffix.lower()
        if extension not in SUPPORTED_DOCUMENT_EXTENSIONS:
            raise DocumentIngestionError(
                f"Unsupported document type '{extension or '(none)'}'. "
                f"Supported types: {', '.join(sorted(SUPPORTED_DOCUMENT_EXTENSIONS))}."
            )
        self._validate_scope_binding(
            scope=scope,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            workflow_id=workflow_id,
        )
        raw = await upload.read()
        if not raw:
            raise DocumentIngestionError("Uploaded document is empty.")

        text = self.extract_text(raw, filename=filename, content_type=upload.content_type)
        if not text.strip():
            raise DocumentIngestionError("No extractable text was found in the uploaded document.")
        chunks = self.chunk_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        if not chunks:
            raise DocumentIngestionError("No text chunks were produced from the uploaded document.")

        document_id = f"doc-{uuid4().hex}"
        storage_uri = self._store_original_document(
            document_id=document_id,
            filename=filename,
            raw=raw,
            user_id=current_user.id,
        )
        memory_ids: list[str] = []
        content_hash = hashlib.sha256(raw).hexdigest()
        for chunk in chunks:
            memory = await self.memory_service.create_memory(
                self._memory_payload(
                    document_id=document_id,
                    filename=filename,
                    content_type=upload.content_type,
                    storage_uri=storage_uri,
                    content_hash=content_hash,
                    scope=scope,
                    current_user=current_user,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    workflow_id=workflow_id,
                    agent_id=agent_id,
                    source=source,
                    tags=tags or [],
                    chunk=chunk,
                    chunk_count=len(chunks),
                ),
                confirmed=True,
                current_user=current_user,
            )
            memory_ids.append(memory.id)

        return DocumentIngestionResult(
            document_id=document_id,
            filename=filename,
            content_type=upload.content_type,
            storage_uri=storage_uri,
            text_characters=len(text),
            chunks_created=len(chunks),
            memory_ids=memory_ids,
        )

    @staticmethod
    def extract_text(raw: bytes, *, filename: str, content_type: str | None = None) -> str:
        extension = Path(filename).suffix.lower()
        if extension in SUPPORTED_TEXT_EXTENSIONS:
            return raw.decode("utf-8", errors="replace")
        if extension == ".pdf":
            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                return "\n\n".join((page.extract_text() or "").strip() for page in pdf.pages).strip()
        if extension == ".docx":
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / filename
                path.write_bytes(raw)
                return docx2txt.process(str(path)).strip()
        raise DocumentIngestionError(f"Unsupported document type: {extension or content_type or '(unknown)'}")

    @staticmethod
    def chunk_text(text: str, *, chunk_size: int = 1200, chunk_overlap: int = 150) -> list[DocumentChunk]:
        normalized = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()
        if not normalized:
            return []
        chunk_size = max(chunk_size, 200)
        chunk_overlap = min(max(chunk_overlap, 0), chunk_size // 2)
        chunks: list[DocumentChunk] = []
        start = 0
        length = len(normalized)
        while start < length:
            target_end = min(start + chunk_size, length)
            end = DocumentIngestionService._chunk_boundary(normalized, start=start, target_end=target_end)
            chunk_text = normalized[start:end].strip()
            if chunk_text:
                chunks.append(DocumentChunk(index=len(chunks), text=chunk_text, start_char=start, end_char=end))
            if end >= length:
                break
            start = max(end - chunk_overlap, 0)
            while start < length and normalized[start].isspace():
                start += 1
        return chunks

    @staticmethod
    def _chunk_boundary(text: str, *, start: int, target_end: int) -> int:
        if target_end >= len(text):
            return len(text)
        window_start = min(max(start + 200, start), target_end)
        for separator in ["\n\n", "\n", ". ", " "]:
            index = text.rfind(separator, window_start, target_end)
            if index > start:
                return index + len(separator)
        return target_end

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = Path(filename).name.strip()
        if not name or name in {".", ".."}:
            raise DocumentIngestionError("Document filename is required.")
        return re.sub(r"[^A-Za-z0-9._ -]", "_", name)

    @staticmethod
    def _validate_scope_binding(
            *,
            scope: str,
            workspace_id: str | None,
            conversation_id: str | None,
            workflow_id: str | None,
    ) -> None:
        if scope == MemoryScope.WORKSPACE.value and not workspace_id:
            raise DocumentIngestionError("workspace_id is required for workspace-scoped document ingestion.")
        if scope == MemoryScope.CONVERSATION.value and not conversation_id:
            raise DocumentIngestionError("conversation_id is required for conversation-scoped document ingestion.")
        if scope == MemoryScope.WORKFLOW.value and not workflow_id:
            raise DocumentIngestionError("workflow_id is required for workflow-scoped document ingestion.")

    @staticmethod
    def _store_original_document(*, document_id: str, filename: str, raw: bytes, user_id: str) -> str | None:
        result = upload_to_s3("documents", document_id, user_id, [raw], [filename])
        uploaded = result.get("uploaded_files")
        if isinstance(uploaded, list) and uploaded:
            return str(uploaded[0])
        return None

    @staticmethod
    def _memory_payload(
            *,
            document_id: str,
            filename: str,
            content_type: str | None,
            storage_uri: str | None,
            content_hash: str,
            scope: str,
            current_user: UserDefinition,
            workspace_id: str | None,
            conversation_id: str | None,
            workflow_id: str | None,
            agent_id: str | None,
            source: str,
            tags: list[str],
            chunk: DocumentChunk,
            chunk_count: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": f"{document_id}-chunk-{chunk.index}",
            "scope": scope,
            "content": chunk.text,
            "summary": f"{filename} chunk {chunk.index + 1}/{chunk_count}",
            "tags": sorted({*tags, "document", Path(filename).suffix.lower().lstrip(".")}),
            "source": source,
            "memory_kind": MemoryKind.ARCHIVE.value,
            "status": "active",
            "importance": 55,
            "agent_id": agent_id,
            "metadata": {
                "document_id": document_id,
                "filename": filename,
                "content_type": content_type,
                "storage_uri": storage_uri,
                "content_sha256": content_hash,
                "chunk_index": chunk.index,
                "chunk_count": chunk_count,
                "start_char": chunk.start_char,
                "end_char": chunk.end_char,
                "semantic_hint": f"Document upload {filename}",
            },
            "sensitive": False,
        }
        if scope == MemoryScope.USER.value:
            payload["created_by_user_id"] = current_user.id
        if scope == MemoryScope.WORKSPACE.value:
            payload["workspace_id"] = workspace_id
        if scope == MemoryScope.CONVERSATION.value:
            payload["conversation_id"] = conversation_id
        if scope == MemoryScope.WORKFLOW.value:
            payload["workflow_id"] = workflow_id
        return payload


__all__ = ["DocumentIngestionError", "DocumentIngestionResult", "DocumentIngestionService"]
