"""Document extraction and archive-memory ingestion pipeline.

Uploaded files are stored through the configured storage backend, text is
extracted with type-specific readers, and chunks are persisted as ``archive``
memory records through ``MemoryService``. This keeps document handling as an
adapter into durable memory rather than a separate knowledge store.
"""

from __future__ import annotations

import docx2txt
import hashlib
import io
import json
import pdfplumber
import re
import tempfile
from dataclasses import dataclass
from fastapi import UploadFile
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Any
from uuid import uuid4

from app.core.storage import upload_to_s3
from app.core.time import utc_now
from app.domain import (
    DocumentUploadMode,
    MemoryType,
    MemoryScope,
    UploadedDocument,
    UploadedDocumentStatus,
    UserDefinition,
)
from app.llm.base import ModelMessage
from app.services.main_agent_setup.service import MainAgentSetupService
from app.services.memory import MemoryService
from app.services.persona_factory import DEFAULT_GOVERNANCE_LABELS, GOVERNANCE_ALLOWED_VALUES
from app.services.source_intelligence import SOURCE_INTELLIGENCE_DOCUMENT_KINDS

SUPPORTED_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".html", ".htm"}
SUPPORTED_DOCUMENT_EXTENSIONS = {*SUPPORTED_TEXT_EXTENSIONS, ".pdf", ".docx"}
DIRECT_CONTEXT_MAX_TOKENS = 24_000
DIRECT_CONTEXT_MAX_CHARACTERS = DIRECT_CONTEXT_MAX_TOKENS * 4


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
    estimated_tokens: int
    upload_mode: str
    context_attachment_id: str | None
    chunks_created: int
    memory_ids: list[str]


@dataclass(slots=True)
class UploadedDocumentDeleteResult:
    document_id: str
    upload_mode: str
    deleted_memory_ids: list[str]
    document_status: str


class DocumentUploadIntelligencePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    document_kind: str = "unknown"
    recommended_scope: str = MemoryScope.USER.value
    recommended_workspace_id: str | None = None
    recommended_conversation_id: str | None = None
    recommended_workflow_id: str | None = None
    recommended_agent_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    chunk_size: int = Field(default=1200, ge=200, le=6000)
    chunk_overlap: int = Field(default=150, ge=0, le=1200)
    governance_labels: dict[str, str] = Field(default_factory=dict)
    confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    rationale: str | None = None

    @model_validator(mode="after")
    def validate_upload_intelligence(self) -> "DocumentUploadIntelligencePayload":
        if self.document_kind not in SOURCE_INTELLIGENCE_DOCUMENT_KINDS:
            self.document_kind = "unknown"
        if self.recommended_scope not in {
            MemoryScope.USER.value,
            MemoryScope.WORKSPACE.value,
            MemoryScope.CONVERSATION.value,
            MemoryScope.WORKFLOW.value,
        }:
            self.recommended_scope = MemoryScope.USER.value
        self.tags = _normalized_tags(self.tags)[:20]
        self.chunk_overlap = min(max(self.chunk_overlap, 0), max(self.chunk_size // 2, 0))
        self.governance_labels = _normalized_governance_labels(self.governance_labels)
        self.summary = self.summary.strip()[:500] or "Uploaded document"
        if self.rationale:
            self.rationale = self.rationale.strip()[:1000]
        return self


@dataclass(slots=True)
class DocumentUploadIntelligenceRecommendation:
    filename: str
    content_type: str | None
    text_characters: int
    source: str
    model_profile_id: str | None
    recommended_scope: str
    recommended_workspace_id: str | None
    recommended_conversation_id: str | None
    recommended_workflow_id: str | None
    recommended_agent_id: str | None
    tags: list[str]
    chunk_size: int
    chunk_overlap: int
    governance_labels: dict[str, str]
    document_kind: str
    summary: str
    confidence: float
    rationale: str | None
    applied: dict[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "content_type": self.content_type,
            "text_characters": self.text_characters,
            "source": self.source,
            "model_profile_id": self.model_profile_id,
            "document_kind": self.document_kind,
            "summary": self.summary,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "recommended": {
                "scope": self.recommended_scope,
                "workspace_id": self.recommended_workspace_id,
                "conversation_id": self.recommended_conversation_id,
                "workflow_id": self.recommended_workflow_id,
                "agent_id": self.recommended_agent_id,
                "tags": self.tags,
                "chunk_size": self.chunk_size,
                "chunk_overlap": self.chunk_overlap,
                "governance_labels": self.governance_labels,
            },
            "applied": self.applied or {},
        }


@dataclass(slots=True)
class DocumentUploadIntelligenceService:
    context: Any

    async def recommend_upload(
            self,
            *,
            filename: str,
            content_type: str | None,
            text: str,
            current_user: UserDefinition,
            current: dict[str, Any] | None = None,
            purpose: str = "memory",
    ) -> DocumentUploadIntelligenceRecommendation:
        current = current or {}
        deterministic = self._deterministic_recommendation(
            filename=filename,
            content_type=content_type,
            text=text,
            current=current,
        )
        try:
            main_agent_profile = await MainAgentSetupService(self.context).require_active_main_agent_profile()
            if not main_agent_profile.default_model_profile_id:
                raise DocumentIngestionError("Active main-agent profile has no default model profile.")
            model_profile = await self.context.model_profile_repo.get(main_agent_profile.default_model_profile_id)
            if model_profile is None:
                raise DocumentIngestionError(
                    f"Main-agent model profile '{main_agent_profile.default_model_profile_id}' was not found."
                )
            payload = await self._generate_with_main_agent_model(
                model_profile=model_profile,
                filename=filename,
                content_type=content_type,
                text=text,
                current=current,
                purpose=purpose,
                deterministic=deterministic,
                current_user=current_user,
            )
            payload = self._honor_available_bindings(payload, current)
            return self._recommendation_from_payload(
                payload,
                filename=filename,
                content_type=content_type,
                text_characters=len(text),
                source="main_agent_llm",
                model_profile_id=model_profile.id,
            )
        except Exception:
            return self._recommendation_from_payload(
                deterministic,
                filename=filename,
                content_type=content_type,
                text_characters=len(text),
                source="deterministic_fallback",
                model_profile_id=None,
            )

    async def _generate_with_main_agent_model(
            self,
            *,
            model_profile,
            filename: str,
            content_type: str | None,
            text: str,
            current: dict[str, Any],
            purpose: str,
            deterministic: DocumentUploadIntelligencePayload,
            current_user: UserDefinition,
    ) -> DocumentUploadIntelligencePayload:
        client = self.context.llm_provider_registry.resolve(model_profile)
        system = (
            "You are Agency's main-agent upload triage assistant. Classify one uploaded document before ingestion. "
            "Recommend memory scope, optional existing agent/workflow/conversation binding, tags, chunking, and "
            "persona governance labels. Return only schema-valid JSON. Use only candidate ids provided in the prompt; "
            "leave ids null when no candidate is clearly appropriate."
        )
        prompt = json.dumps(
            {
                "purpose": purpose,
                "current_input": current,
                "allowed_document_kinds": sorted(SOURCE_INTELLIGENCE_DOCUMENT_KINDS),
                "allowed_scopes": [
                    MemoryScope.USER.value,
                    MemoryScope.WORKSPACE.value,
                    MemoryScope.CONVERSATION.value,
                    MemoryScope.WORKFLOW.value,
                ],
                "allowed_governance_values": {
                    key: sorted(values)
                    for key, values in sorted(GOVERNANCE_ALLOWED_VALUES.items())
                    if key in DEFAULT_GOVERNANCE_LABELS
                },
                "fallback": deterministic.model_dump(mode="json"),
                "candidates": await self._candidate_bindings(current_user=current_user),
                "document": {
                    "filename": filename,
                    "content_type": content_type,
                    "text_characters": len(text),
                    "sample": text[:8000],
                },
            },
            ensure_ascii=True,
        )
        messages = [ModelMessage(role="system", content=system), ModelMessage(role="user", content=prompt)]
        if hasattr(client, "agenerate_structured"):
            response = await client.agenerate_structured(
                messages,
                schema=DocumentUploadIntelligencePayload.model_json_schema(),
                schema_name="document_upload_intelligence",
                temperature=model_profile.temperature,
                max_tokens=model_profile.max_tokens,
            )
        else:
            import asyncio

            response = await asyncio.to_thread(
                client.generate_structured,
                messages,
                schema=DocumentUploadIntelligencePayload.model_json_schema(),
                schema_name="document_upload_intelligence",
                temperature=model_profile.temperature,
                max_tokens=model_profile.max_tokens,
            )
        if not isinstance(response.content, dict):
            raise DocumentIngestionError("Main-agent upload intelligence response was not an object.")
        return DocumentUploadIntelligencePayload.model_validate(response.content)

    async def _candidate_bindings(self, *, current_user: UserDefinition) -> dict[str, Any]:
        def agent_payload(agent: Any) -> dict[str, Any]:
            return {
                "id": getattr(agent, "id", None),
                "name": getattr(agent, "display_name", None) or getattr(agent, "name", None),
                "description": getattr(agent, "description", None),
                "role": getattr(agent, "role", None),
            }

        def workflow_payload(workflow: Any) -> dict[str, Any]:
            return {
                "id": getattr(workflow, "id", None),
                "name": getattr(workflow, "name", None),
                "description": getattr(workflow, "description", None),
            }

        def conversation_payload(conversation: Any) -> dict[str, Any]:
            return {
                "id": getattr(conversation, "id", None),
                "title": getattr(conversation, "title", None),
                "channel_type": getattr(conversation, "channel_type", None),
            }

        agents = await _repo_list(getattr(self.context, "agent_repo", None))
        workflows = await _repo_list(getattr(self.context, "workflow_repo", None))
        conversations = await _repo_list(getattr(self.context, "conversation_repo", None))
        return {
            "agents": [agent_payload(item) for item in agents[:40]],
            "workflows": [workflow_payload(item) for item in workflows[:40]],
            "conversations": [conversation_payload(item) for item in conversations[:40]],
            "current_user_id": current_user.id,
        }

    @staticmethod
    def _honor_available_bindings(
            payload: DocumentUploadIntelligencePayload,
            current: dict[str, Any],
    ) -> DocumentUploadIntelligencePayload:
        if payload.recommended_scope == MemoryScope.WORKSPACE.value and not (
                payload.recommended_workspace_id or current.get("workspace_id")
        ):
            payload.recommended_scope = current.get("scope") or MemoryScope.USER.value
        if payload.recommended_scope == MemoryScope.CONVERSATION.value and not (
                payload.recommended_conversation_id or current.get("conversation_id")
        ):
            payload.recommended_scope = current.get("scope") or MemoryScope.USER.value
        if payload.recommended_scope == MemoryScope.WORKFLOW.value and not (
                payload.recommended_workflow_id or current.get("workflow_id")
        ):
            payload.recommended_scope = current.get("scope") or MemoryScope.USER.value
        return payload

    @staticmethod
    def _deterministic_recommendation(
            *,
            filename: str,
            content_type: str | None,
            text: str,
            current: dict[str, Any],
    ) -> DocumentUploadIntelligencePayload:
        lowered = f"{filename}\n{text[:4000]}".lower()
        extension = Path(filename).suffix.lower().lstrip(".")
        document_kind = "unknown"
        if any(token in lowered for token in ("policy", "procedure", "sop", "standard", "runbook")):
            document_kind = "policy_sop"
        elif any(token in lowered for token in ("from:", "to:", "subject:", "forwarded message")):
            document_kind = "email_thread"
        elif any(token in lowered for token in ("slack", "teams", "whatsapp", "chat", "dm")):
            document_kind = "chat_export"
        elif any(token in lowered for token in ("ticket", "incident", "jira", "servicenow")):
            document_kind = "ticket"
        elif extension in {"py", "ts", "tsx", "js", "json"} or "```" in lowered:
            document_kind = "code"
        elif any(token in lowered for token in ("meeting", "minutes", "attendees", "action item")):
            document_kind = "meeting_note"
        elif any(token in lowered for token in ("workpaper", "testing", "evidence")):
            document_kind = "workpaper"
        elif any(token in lowered for token in ("report", "observation", "recommendation")):
            document_kind = "report"
        chunk_size, chunk_overlap = _chunking_for_document_kind(document_kind, len(text))
        tags = _normalized_tags([
            "document",
            extension,
            document_kind,
            *_filename_tags(filename),
            *list(current.get("tags") or []),
        ])
        governance = dict(DEFAULT_GOVERNANCE_LABELS)
        governance["source_basis"] = "uploaded_private_material"
        if document_kind == "chat_export":
            governance["source_basis"] = "chat_export"
            governance["persona_type"] = "personal"
            governance["capability_mode"] = "persona_only"
            governance["consent_status"] = "unverified_private_person"
            governance["sensitivity_level"] = "intimate"
        elif any(token in lowered for token in ("confidential", "restricted", "regulated", "pii", "personal data")):
            governance["sensitivity_level"] = "sensitive"
        return DocumentUploadIntelligencePayload(
            summary=f"{filename} appears to be a {document_kind.replace('_', ' ')} document.",
            document_kind=document_kind,
            recommended_scope=current.get("scope") or MemoryScope.USER.value,
            recommended_workspace_id=current.get("workspace_id"),
            recommended_conversation_id=current.get("conversation_id"),
            recommended_workflow_id=current.get("workflow_id"),
            recommended_agent_id=current.get("agent_id"),
            tags=tags,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            governance_labels=governance,
            confidence=0.55,
            rationale="Deterministic filename and content keyword fallback.",
        )

    @staticmethod
    def _recommendation_from_payload(
            payload: DocumentUploadIntelligencePayload,
            *,
            filename: str,
            content_type: str | None,
            text_characters: int,
            source: str,
            model_profile_id: str | None,
    ) -> DocumentUploadIntelligenceRecommendation:
        return DocumentUploadIntelligenceRecommendation(
            filename=filename,
            content_type=content_type,
            text_characters=text_characters,
            source=source,
            model_profile_id=model_profile_id,
            recommended_scope=payload.recommended_scope,
            recommended_workspace_id=payload.recommended_workspace_id,
            recommended_conversation_id=payload.recommended_conversation_id,
            recommended_workflow_id=payload.recommended_workflow_id,
            recommended_agent_id=payload.recommended_agent_id,
            tags=payload.tags,
            chunk_size=payload.chunk_size,
            chunk_overlap=payload.chunk_overlap,
            governance_labels=payload.governance_labels,
            document_kind=payload.document_kind,
            summary=payload.summary,
            confidence=payload.confidence,
            rationale=payload.rationale,
        )


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
            chunk_size: int | None = None,
            chunk_overlap: int | None = None,
            auto_intelligence: bool = False,
            allow_scope_suggestion: bool = False,
            allow_agent_suggestion: bool = False,
            purpose: str = "memory",
            upload_mode: str = DocumentUploadMode.VECTOR.value,
    ) -> DocumentIngestionResult:
        parsed_upload_mode = self._validate_upload_mode(upload_mode)
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
        estimated_tokens = self.estimate_tokens(text)
        if parsed_upload_mode in {DocumentUploadMode.CONTEXT, DocumentUploadMode.BOTH}:
            self._validate_direct_context_size(estimated_tokens)
        upload_intelligence: DocumentUploadIntelligenceRecommendation | None = None
        if auto_intelligence:
            upload_intelligence = await DocumentUploadIntelligenceService(self.context).recommend_upload(
                filename=filename,
                content_type=upload.content_type,
                text=text,
                current_user=current_user,
                purpose=purpose,
                current={
                    "scope": scope,
                    "workspace_id": workspace_id,
                    "conversation_id": conversation_id,
                    "workflow_id": workflow_id,
                    "agent_id": agent_id,
                    "tags": tags or [],
                    "chunk_size": chunk_size,
                    "chunk_overlap": chunk_overlap,
                },
            )
            applied = self._apply_upload_intelligence(
                upload_intelligence,
                scope=scope,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                tags=tags or [],
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                allow_scope_suggestion=allow_scope_suggestion,
                allow_agent_suggestion=allow_agent_suggestion,
            )
            scope = applied["scope"]
            workspace_id = applied["workspace_id"]
            conversation_id = applied["conversation_id"]
            workflow_id = applied["workflow_id"]
            agent_id = applied["agent_id"]
            tags = applied["tags"]
            chunk_size = applied["chunk_size"]
            chunk_overlap = applied["chunk_overlap"]
            upload_intelligence.applied = applied
            self._validate_scope_binding(
                scope=scope,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                workflow_id=workflow_id,
            )
        document_id = f"doc-{uuid4().hex}"
        storage_uri = self._store_original_document(
            document_id=document_id,
            filename=filename,
            raw=raw,
            user_id=current_user.id,
        )
        content_hash = hashlib.sha256(raw).hexdigest()
        await self._store_uploaded_document(
            UploadedDocument(
                id=document_id,
                filename=filename,
                content_type=upload.content_type,
                storage_uri=storage_uri,
                # The extracted text is stored on the document record, not in
                # conversation message metadata, so direct context can be loaded
                # by reference without bloating chat history.
                extracted_text=text[:DIRECT_CONTEXT_MAX_CHARACTERS],
                content_sha256=content_hash,
                text_characters=len(text),
                estimated_tokens=estimated_tokens,
                upload_mode=parsed_upload_mode,
                scope=scope,
                created_by_user_id=current_user.id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                metadata={
                    "source": source,
                    "tags": tags or [],
                    "upload_intelligence": upload_intelligence.as_payload()
                    if upload_intelligence is not None
                    else None,
                    "stored_text_truncated": len(text) > DIRECT_CONTEXT_MAX_CHARACTERS,
                    "upload_observability": self._upload_observability_metadata(
                        document_id=document_id,
                        upload_mode=parsed_upload_mode,
                        text_characters=len(text),
                        estimated_tokens=estimated_tokens,
                    ),
                },
            )
        )
        memory_ids: list[str] = []
        chunks: list[DocumentChunk] = []
        projection_event_created = False
        if parsed_upload_mode in {DocumentUploadMode.VECTOR, DocumentUploadMode.BOTH}:
            chunk_size = chunk_size or 1200
            chunk_overlap = 150 if chunk_overlap is None else chunk_overlap
            chunks = self.chunk_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
            if not chunks:
                raise DocumentIngestionError("No text chunks were produced from the uploaded document.")
            # Context-only uploads are prompt attachments; only memory-backed uploads enter retrieval and graph projection.
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
                        upload_mode=parsed_upload_mode,
                        upload_intelligence=upload_intelligence,
                        chunk=chunk,
                        chunk_count=len(chunks),
                    ),
                    confirmed=True,
                    current_user=current_user,
                )
                memory_ids.append(memory.id)
            memory_id_set = set(memory_ids)
            created_memories = [item for item in await self.context.memory_repo.list() if item.id in memory_id_set]
            await self.memory_service.append_document_collection_projection_event(
                "document_memory_collection.created",
                document_id=document_id,
                memories=created_memories,
            )
            projection_event_created = True

        await self._update_uploaded_document_metadata(
            document_id,
            {
                "upload_observability": self._upload_observability_metadata(
                    document_id=document_id,
                    upload_mode=parsed_upload_mode,
                    text_characters=len(text),
                    estimated_tokens=estimated_tokens,
                    chunks_created=len(chunks),
                    memory_ids=memory_ids,
                    projection_event_created=projection_event_created,
                )
            },
        )

        return DocumentIngestionResult(
            document_id=document_id,
            filename=filename,
            content_type=upload.content_type,
            storage_uri=storage_uri,
            text_characters=len(text),
            estimated_tokens=estimated_tokens,
            upload_mode=parsed_upload_mode.value,
            context_attachment_id=(
                document_id
                if parsed_upload_mode in {DocumentUploadMode.CONTEXT, DocumentUploadMode.BOTH}
                else None
            ),
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
    def estimate_tokens(text: str) -> int:
        return max(1, (len(text) + 3) // 4)

    @staticmethod
    def _validate_upload_mode(upload_mode: str | None) -> DocumentUploadMode:
        value = (upload_mode or DocumentUploadMode.VECTOR.value).strip().lower()
        try:
            return DocumentUploadMode(value)
        except ValueError as exc:
            raise DocumentIngestionError("Invalid upload_mode. Choose vector, context, or both.") from exc

    @staticmethod
    def _validate_direct_context_size(estimated_tokens: int) -> None:
        if estimated_tokens > DIRECT_CONTEXT_MAX_TOKENS:
            raise DocumentIngestionError(
                "Direct context upload is too large. Use Save for retrieval or split the document."
            )

    async def _store_uploaded_document(self, document: UploadedDocument) -> UploadedDocument:
        repo = getattr(self.context, "uploaded_document_repo", None)
        if repo is None or not hasattr(repo, "create"):
            return document
        return await repo.create(document)

    async def _update_uploaded_document_metadata(self, document_id: str, metadata: dict[str, Any]) -> None:
        repo = getattr(self.context, "uploaded_document_repo", None)
        if repo is None or not hasattr(repo, "get") or not hasattr(repo, "save"):
            return
        document = await repo.get(document_id)
        if document is None:
            return
        updated = document.model_copy(
            update={
                "metadata": {**(document.metadata or {}), **metadata},
                "updated_at": utc_now(),
            }
        )
        await repo.save(updated)

    async def delete_uploaded_document(
            self,
            document_id: str,
            *,
            current_user: UserDefinition,
    ) -> UploadedDocumentDeleteResult | None:
        repo = getattr(self.context, "uploaded_document_repo", None)
        if repo is None or not hasattr(repo, "get") or not hasattr(repo, "save"):
            return None
        document = await repo.get(document_id)
        if document is None:
            return None
        if not self._can_delete_uploaded_document(document, current_user):
            return None

        mode = document.upload_mode.value if hasattr(document.upload_mode, "value") else str(document.upload_mode)
        deleted_memory_ids: list[str] = []
        if document.upload_mode in {DocumentUploadMode.VECTOR, DocumentUploadMode.BOTH}:
            deleted_memory_ids = await self.memory_service.delete_document_memories(
                document.id,
                current_user=current_user,
            )
        metadata = dict(document.metadata or {})
        observability = dict(metadata.get("upload_observability") or {})
        observability.update(
            {
                "deleted": True,
                "deleted_at": utc_now().isoformat(),
                "deleted_memory_ids": deleted_memory_ids,
                "deleted_memory_count": len(deleted_memory_ids),
            }
        )
        metadata["upload_observability"] = observability
        metadata["deleted_at"] = observability["deleted_at"]
        metadata["deleted_memory_ids"] = deleted_memory_ids
        # Remove extracted text from the active document record so a deleted context attachment
        # can no longer be rehydrated into a prompt through a stale message reference.
        deleted_document = document.model_copy(
            update={
                "status": UploadedDocumentStatus.DELETED,
                "extracted_text": None,
                "metadata": metadata,
                "updated_at": utc_now(),
            }
        )
        saved = await repo.save(deleted_document)
        return UploadedDocumentDeleteResult(
            document_id=saved.id,
            upload_mode=mode,
            deleted_memory_ids=deleted_memory_ids,
            document_status=saved.status.value,
        )

    @staticmethod
    def _can_delete_uploaded_document(document: UploadedDocument, current_user: UserDefinition) -> bool:
        if "admin" in current_user.roles:
            return True
        return document.created_by_user_id == current_user.id

    @staticmethod
    def _upload_observability_metadata(
            *,
            document_id: str,
            upload_mode: DocumentUploadMode,
            text_characters: int,
            estimated_tokens: int,
            chunks_created: int = 0,
            memory_ids: list[str] | None = None,
            projection_event_created: bool = False,
    ) -> dict[str, Any]:
        direct_context_attachment = upload_mode in {DocumentUploadMode.CONTEXT, DocumentUploadMode.BOTH}
        memory_ids = memory_ids or []
        return {
            "upload_mode": upload_mode.value,
            "text_characters": text_characters,
            "estimated_tokens": estimated_tokens,
            "direct_context_attachment": direct_context_attachment,
            "context_attachment_id": document_id if direct_context_attachment else None,
            "direct_context_max_tokens": DIRECT_CONTEXT_MAX_TOKENS if direct_context_attachment else None,
            "archive_memory_created": bool(memory_ids),
            "chunks_created": chunks_created,
            "memory_ids": memory_ids,
            "projection_event_created": projection_event_created,
        }

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
            upload_mode: DocumentUploadMode,
            upload_intelligence: DocumentUploadIntelligenceRecommendation | None,
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
            "memory_type": MemoryType.ARCHIVE.value,
            "status": "active",
            "importance": 55,
            "agent_id": agent_id,
            "metadata": {
                "document_id": document_id,
                "filename": filename,
                "content_type": content_type,
                "storage_uri": storage_uri,
                "content_sha256": content_hash,
                "upload_mode": upload_mode.value,
                "chunk_index": chunk.index,
                "chunk_count": chunk_count,
                "start_char": chunk.start_char,
                "end_char": chunk.end_char,
                "semantic_hint": f"Document upload {filename}",
            },
            "sensitive": False,
        }
        if upload_intelligence is not None:
            payload["metadata"]["upload_intelligence"] = upload_intelligence.as_payload()
        if scope == MemoryScope.USER.value:
            payload["created_by_user_id"] = current_user.id
        if scope == MemoryScope.WORKSPACE.value:
            payload["workspace_id"] = workspace_id
        if scope == MemoryScope.CONVERSATION.value:
            payload["conversation_id"] = conversation_id
        if scope == MemoryScope.WORKFLOW.value:
            payload["workflow_id"] = workflow_id
        return payload

    @staticmethod
    def _apply_upload_intelligence(
            recommendation: DocumentUploadIntelligenceRecommendation,
            *,
            scope: str,
            workspace_id: str | None,
            conversation_id: str | None,
            workflow_id: str | None,
            agent_id: str | None,
            tags: list[str],
            chunk_size: int | None,
            chunk_overlap: int | None,
            allow_scope_suggestion: bool,
            allow_agent_suggestion: bool,
    ) -> dict[str, Any]:
        next_scope = scope
        next_workspace_id = workspace_id
        next_conversation_id = conversation_id
        next_workflow_id = workflow_id
        if allow_scope_suggestion:
            next_scope = recommendation.recommended_scope or scope
            next_workspace_id = recommendation.recommended_workspace_id or workspace_id
            next_conversation_id = recommendation.recommended_conversation_id or conversation_id
            next_workflow_id = recommendation.recommended_workflow_id or workflow_id
        next_agent_id = recommendation.recommended_agent_id if allow_agent_suggestion else agent_id
        return {
            "scope": next_scope,
            "workspace_id": next_workspace_id,
            "conversation_id": next_conversation_id,
            "workflow_id": next_workflow_id,
            "agent_id": next_agent_id or agent_id,
            "tags": _normalized_tags([*tags, *recommendation.tags]),
            "chunk_size": chunk_size or recommendation.chunk_size,
            "chunk_overlap": chunk_overlap if chunk_overlap is not None else recommendation.chunk_overlap,
            "applied_at": utc_now().isoformat(),
        }


async def _repo_list(repo: Any) -> list[Any]:
    if repo is None or not hasattr(repo, "list"):
        return []
    try:
        items = await repo.list()
    except TypeError:
        items = await repo.list(limit=40)
    return list(items or [])


def _filename_tags(filename: str) -> list[str]:
    stem = Path(filename).stem.lower()
    return [item for item in re.split(r"[^a-z0-9]+", stem) if len(item) >= 3][:8]


def _normalized_tags(values: list[Any]) -> list[str]:
    tags: list[str] = []
    for value in values:
        text = str(value or "").strip().lower().replace(" ", "-")
        text = re.sub(r"[^a-z0-9._:-]+", "-", text).strip("-")
        if text and text not in tags:
            tags.append(text)
    return tags


def _chunking_for_document_kind(document_kind: str, text_length: int) -> tuple[int, int]:
    if document_kind == "code":
        return 2200, 250
    if document_kind in {"email_thread", "chat_export"}:
        return 1000, 200
    if document_kind in {"policy_sop", "workpaper", "report"}:
        return 1400 if text_length > 12000 else 1200, 180
    if document_kind in {"ticket", "meeting_note"}:
        return 900, 120
    return 1600 if text_length > 20000 else 1200, 150


def _normalized_governance_labels(labels: dict[str, Any]) -> dict[str, str]:
    normalized = dict(DEFAULT_GOVERNANCE_LABELS)
    for key, value in labels.items():
        if key not in DEFAULT_GOVERNANCE_LABELS:
            continue
        text = str(value or "").strip()
        if text and text in GOVERNANCE_ALLOWED_VALUES.get(key, set()):
            normalized[key] = text
    if normalized["visibility"] == "marketplace":
        normalized["visibility"] = "private"
    if normalized["sensitivity_level"] == "intimate":
        normalized["visibility"] = "private"
    if normalized["persona_type"] == "personal" and normalized["consent_status"] == "unspecified":
        normalized["consent_status"] = "unverified_private_person"
    return normalized


__all__ = [
    "DocumentIngestionError",
    "DocumentIngestionResult",
    "DocumentIngestionService",
    "DocumentUploadIntelligenceRecommendation",
    "DocumentUploadIntelligenceService",
]
