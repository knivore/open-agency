from __future__ import annotations

from datetime import datetime
from pydantic import Field
from typing import Literal

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


class ConnectorSetupGuideDefinition(DomainModel):
    storagePath: str
    fields: list[ConnectorSetupGuideFieldDefinition] = Field(default_factory=list)
    agencyStores: list[str] = Field(default_factory=list)
    completionSignal: str


class ConnectorCapabilityDefinition(DomainModel):
    backendKey: str
    displayName: str
    authModel: str
    providerAliases: list[str] = Field(default_factory=list)
    healthSupported: bool = False
    requiredMetadata: list[ConnectorMetadataRequirementDefinition] = Field(default_factory=list)
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
