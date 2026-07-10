"""Domain contracts for the integration registry and connector health payloads."""

from __future__ import annotations

from datetime import datetime
from pydantic import Field, field_validator, model_validator
from typing import Any, Literal
from uuid import uuid4

from app.integrations.connectors import normalize_connector_provider_key, validate_connector_metadata
from .credentials import DomainModel


class PlannedIntegrationDefinition(DomainModel):
    backendKey: str
    authModel: str
    summary: str
    launchPriority: Literal["now", "next", "later"] | None = None
    providerAliases: list[str] = Field(default_factory=list)


class IntegrationRegistryCategoryDefinition(DomainModel):
    id: str
    name: str
    description: str
    providers: dict[str, PlannedIntegrationDefinition]


class IntegrationRegistryPayload(DomainModel):
    categories: list[IntegrationRegistryCategoryDefinition]
    updated_at: datetime | None = None


class ConnectorMetadataRequirementDefinition(DomainModel):
    key: str
    description: str


class ConnectorSetupGuideFieldDefinition(DomainModel):
    key: str
    label: str
    secret: bool = True
    description: str


class ConnectorSetupGuideOptionDefinition(DomainModel):
    id: str
    name: str
    authModel: str
    summary: str
    fields: list[ConnectorSetupGuideFieldDefinition] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ConnectorSetupGuideDefinition(DomainModel):
    storagePath: str
    fields: list[ConnectorSetupGuideFieldDefinition] = Field(default_factory=list)
    options: list[ConnectorSetupGuideOptionDefinition] = Field(default_factory=list)
    agencyStores: list[str] = Field(default_factory=list)
    completionSignal: str
    notes: list[str] = Field(default_factory=list)


class ConnectorCapabilityDefinition(DomainModel):
    backendKey: str
    displayName: str
    authModel: str
    providerAliases: list[str] = Field(default_factory=list)
    capabilitySurface: Literal["connector", "module"] = "connector"
    moduleCapabilities: list[str] = Field(default_factory=list)
    dependsOnAgencyCapabilities: list[str] = Field(default_factory=list)
    ownershipNotes: list[str] = Field(default_factory=list)
    onecliTransportMode: OneCLITransportMode = "proxy"
    healthSupported: bool = False
    requiredMetadata: list[ConnectorMetadataRequirementDefinition] = Field(default_factory=list)
    instanceIdentityMetadata: list[ConnectorMetadataRequirementDefinition] = Field(default_factory=list)
    targetScopeMetadata: list[ConnectorMetadataRequirementDefinition] = Field(default_factory=list)
    supportedSecretRefSchemes: list[str] = Field(default_factory=list)
    onecliSetupGuide: ConnectorSetupGuideDefinition | None = None


class ConnectorCapabilitiesPayload(DomainModel):
    connectors: dict[str, ConnectorCapabilityDefinition]
    updated_at: datetime | None = None


class ConnectorCredentialValidationPayload(DomainModel):
    provider: str
    valid: bool
    errors: list[str] = Field(default_factory=list)
    capability: ConnectorCapabilityDefinition | None = None


class ConnectorInstallationStatus(str):
    SETUP_PENDING = "setup_pending"
    ACTIVE = "active"
    REVOKED = "revoked"
    DISABLED = "disabled"
    ROTATION_REQUIRED = "rotation_required"


class ConnectorInstallation(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    owner_user_id: str
    workflow_id: str | None = None
    provider: str
    name: str
    onecli_credential_ref: str
    runtime_secret_encrypted: str | None = Field(default=None, exclude=True)
    status: Literal["setup_pending", "active", "revoked", "disabled", "rotation_required"] = "setup_pending"
    setup_session_id: str | None = None
    last_rotated_at: datetime | None = None
    revoked_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, str):
            return value
        return normalize_connector_provider_key(value)

    @model_validator(mode="after")
    def _validate_connector_metadata(self) -> "ConnectorInstallation":
        if self.status == ConnectorInstallationStatus.SETUP_PENDING:
            return self
        errors = validate_connector_metadata(self.provider, self.metadata)
        if errors:
            raise ValueError(errors[0])
        return self


class ConnectorSetupSessionPayload(DomainModel):
    installation: ConnectorInstallation
    setup_url: str
    device_code: str
    onecli_credential_ref: str
    expires_at: datetime | None = None


class ConnectorHealthHistoryItem(DomainModel):
    executionId: str
    credentialId: str
    credentialName: str
    provider: str
    status: str
    startedAt: datetime | None = None
    completedAt: datetime | None = None
    error: str | None = None
    eventTypes: list[str] = Field(default_factory=list)


class ConnectorHealthHistoryPayload(DomainModel):
    items: list[ConnectorHealthHistoryItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0
    status: str | None = None
    startedAfter: datetime | None = None
    startedBefore: datetime | None = None


class ConnectorHealthHistoryPrunePayload(DomainModel):
    deleted: int = 0
    matched: int = 0
    retained: int = 0
    status: str | None = None
    provider: str | None = None
    startedBefore: datetime | None = None
    keepLatest: int | None = None
    credentialId: str | None = None


class ConnectorHealthRetentionRunPayload(DomainModel):
    scanned: int = 0
    matched: int = 0
    deleted: int = 0
    retained: int = 0
    startedBefore: datetime | None = None
    keepLatestPerCredential: int = 0


class ConnectorHealthRetentionStatusPayload(DomainModel):
    enabled: bool
    intervalSeconds: int
    retentionDays: int
    maxPerCredential: int
    counters: dict[str, int] = Field(default_factory=dict)
    lastRun: dict | None = None


OneCLITransportMode = Literal["proxy", "direct"]
