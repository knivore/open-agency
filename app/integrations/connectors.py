from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass(frozen=True, slots=True)
class ConnectorHealthRequest:
    method: str
    url_template: str
    auth_scheme: str
    query_params: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ConnectorHealthCheck:
    request: ConnectorHealthRequest
    success_field: str | None = None
    success_value_from_metadata: str | None = None
    success_bool_field: str | None = None
    error_field: str | None = None
    error_nested_field: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class ConnectorRequirement:
    metadata_key: str
    description: str


@dataclass(frozen=True, slots=True)
class ConnectorDefinition:
    key: str
    aliases: tuple[str, ...] = ()
    required_metadata: tuple[ConnectorRequirement, ...] = ()
    health_check: ConnectorHealthCheck | None = None


_CONNECTOR_DEFINITIONS: tuple[ConnectorDefinition, ...] = (
    ConnectorDefinition(
        key="telegram-bot",
        aliases=("telegram",),
        health_check=ConnectorHealthCheck(
            request=ConnectorHealthRequest(
                method="GET",
                url_template="https://api.telegram.org/bot{token}/getMe",
                auth_scheme="none",
            ),
            success_bool_field="ok",
            error_field="description",
        ),
    ),
    ConnectorDefinition(
        key="whatsapp-cloud-api",
        aliases=("whatsapp", "meta-whatsapp"),
        required_metadata=(
            ConnectorRequirement(
                metadata_key="phone_number_id",
                description="WhatsApp Cloud API credentials require metadata.phone_number_id.",
            ),
        ),
        health_check=ConnectorHealthCheck(
            request=ConnectorHealthRequest(
                method="GET",
                url_template="https://graph.facebook.com/{api_version}/{phone_number_id}",
                auth_scheme="bearer",
                query_params={"fields": "id,display_phone_number,verified_name"},
            ),
            success_field="id",
            success_value_from_metadata="phone_number_id",
            error_nested_field=("error", "message"),
        ),
    ),
    ConnectorDefinition(
        key="discord-bot",
        aliases=("discord",),
        health_check=ConnectorHealthCheck(
            request=ConnectorHealthRequest(
                method="GET",
                url_template="https://discord.com/api/v10/users/@me",
                auth_scheme="bot",
            ),
            success_field="id",
            error_field="message",
        ),
    ),
    ConnectorDefinition(key="slack-app", aliases=("slack",)),
    ConnectorDefinition(key="microsoft-teams", aliases=("teams",)),
    ConnectorDefinition(key="twilio-sms", aliases=("twilio",)),
    ConnectorDefinition(key="gmail", aliases=("google-mail",)),
    ConnectorDefinition(key="outlook-email", aliases=("outlook", "microsoft-outlook")),
    ConnectorDefinition(key="notion"),
    ConnectorDefinition(key="linear"),
    ConnectorDefinition(key="jira", aliases=("atlassian-jira",)),
    ConnectorDefinition(key="confluence", aliases=("atlassian-confluence",)),
    ConnectorDefinition(key="airtable"),
    ConnectorDefinition(key="google-workspace",
                        aliases=("google-drive", "google-calendar", "google-docs", "google-sheets")),
    ConnectorDefinition(key="microsoft-365", aliases=("office365", "sharepoint", "onedrive")),
    ConnectorDefinition(key="github"),
    ConnectorDefinition(key="gitlab"),
    ConnectorDefinition(key="sentry"),
    ConnectorDefinition(key="pagerduty"),
    ConnectorDefinition(key="figma"),
    ConnectorDefinition(key="canva"),
    ConnectorDefinition(key="youtube", aliases=("google-youtube",)),
    ConnectorDefinition(key="adobe-creative-cloud", aliases=("adobe", "creative-cloud")),
    ConnectorDefinition(key="perplexity"),
    ConnectorDefinition(key="tavily"),
    ConnectorDefinition(key="wikipedia"),
    ConnectorDefinition(key="s3", aliases=("aws-s3",)),
    ConnectorDefinition(key="google-drive", aliases=("google-workspace-drive",)),
    ConnectorDefinition(key="dropbox"),
    ConnectorDefinition(key="onedrive", aliases=("microsoft-onedrive",)),
    ConnectorDefinition(key="sharepoint", aliases=("microsoft-sharepoint",)),
)


@lru_cache(maxsize=1)
def connector_alias_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for definition in _CONNECTOR_DEFINITIONS:
        lookup[definition.key] = definition.key
        for alias in definition.aliases:
            lookup[alias] = definition.key
    return lookup


@lru_cache(maxsize=1)
def connector_definition_lookup() -> dict[str, ConnectorDefinition]:
    return {definition.key: definition for definition in _CONNECTOR_DEFINITIONS}


def normalize_connector_provider_key(provider: str | None) -> str | None:
    if provider is None:
        return None
    normalized = provider.strip().lower()
    if not normalized:
        return None
    return connector_alias_lookup().get(normalized, normalized)


def get_connector_definition(provider: str | None) -> ConnectorDefinition | None:
    normalized = normalize_connector_provider_key(provider)
    if normalized is None:
        return None
    return connector_definition_lookup().get(normalized)


def display_connector_provider_key(provider: str | None) -> str | None:
    definition = get_connector_definition(provider)
    if definition is None:
        normalized = normalize_connector_provider_key(provider)
        return normalized
    if definition.aliases:
        return definition.aliases[0]
    return definition.key


def validate_connector_metadata(provider: str | None, metadata: dict[str, Any]) -> list[str]:
    definition = get_connector_definition(provider)
    if definition is None:
        return []

    errors: list[str] = []
    for requirement in definition.required_metadata:
        value = metadata.get(requirement.metadata_key)
        if not isinstance(value, str) or not value.strip():
            errors.append(requirement.description)
    return errors


def connector_health_supported(provider: str | None) -> bool:
    definition = get_connector_definition(provider)
    return definition is not None and definition.health_check is not None
