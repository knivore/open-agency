"""Domain contracts for credentials, secret references, and model providers."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic import field_validator, model_validator
from typing import Any, Dict, Optional
from uuid import uuid4

from app.integrations.connectors import normalize_connector_provider_key, validate_connector_metadata


class DomainModel(BaseModel):
    """Base model for strict API/domain contracts with alias-friendly input."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )


class ProviderEndpointDefinition(DomainModel):
    base_url: Optional[str] = None
    api_version: Optional[str] = None
    region: Optional[str] = None
    headers: Dict[str, str] = Field(default_factory=dict)


class SecretReference(DomainModel):
    secret_name: str
    source: Optional[str] = None
    description: Optional[str] = None


class CredentialReference(DomainModel):
    ref: str
    source: Optional[str] = None
    key: Optional[str] = None
    description: Optional[str] = None


class ConnectorBindingDefinition(DomainModel):
    provider: str
    credential_id: str = Field(validation_alias=AliasChoices("credential_id", "ref"),
                               serialization_alias="credential_id")
    purpose: Optional[str] = None
    target_scope: Dict[str, Any] = Field(default_factory=dict)
    identity_summary: Optional[str] = None

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, str):
            return value
        return normalize_connector_provider_key(value)


class CredentialStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    DISABLED = "disabled"
    ROTATION_REQUIRED = "rotation_required"


class CredentialDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    owner_user_id: Optional[str] = None
    name: str
    provider: Optional[str] = None
    secret_ref: str
    status: CredentialStatus = CredentialStatus.ACTIVE
    last_rotated_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    secret_version: int = 1
    rotation_policy: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        return normalize_connector_provider_key(value)

    @model_validator(mode="after")
    def _validate_connector_metadata(self) -> "CredentialDefinition":
        errors = validate_connector_metadata(self.provider, self.metadata)
        if errors:
            raise ValueError(errors[0])

        return self
