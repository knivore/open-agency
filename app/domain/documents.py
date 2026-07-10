"""Domain contracts for uploaded documents.

Uploaded documents are intentionally separate from archive memory chunks. A
document can be used as immediate model context, durable retrieval memory, or
both, while the raw extracted text remains addressable by document reference.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pydantic import Field
from typing import Any

from .credentials import DomainModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DocumentUploadMode(str, Enum):
    VECTOR = "vector"
    CONTEXT = "context"
    BOTH = "both"


class UploadedDocumentStatus(str, Enum):
    ACTIVE = "active"
    DELETED = "deleted"


class UploadedDocument(DomainModel):
    id: str
    filename: str
    content_type: str | None = None
    storage_uri: str | None = None
    extracted_text: str | None = None
    content_sha256: str
    text_characters: int
    estimated_tokens: int
    upload_mode: DocumentUploadMode = DocumentUploadMode.VECTOR
    scope: str
    created_by_user_id: str | None = None
    workspace_id: str | None = None
    conversation_id: str | None = None
    workflow_id: str | None = None
    agent_id: str | None = None
    status: UploadedDocumentStatus = UploadedDocumentStatus.ACTIVE
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
