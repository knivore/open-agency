"""Service that exposes the static integration and connector capability registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain import (
    ConnectorCapabilitiesPayload,
    ConnectorCapabilityDefinition,
    ConnectorMetadataRequirementDefinition,
    ConnectorSetupGuideDefinition,
    ConnectorSetupGuideFieldDefinition,
    ConnectorSetupGuideOptionDefinition,
    IntegrationRegistryCategoryDefinition,
    IntegrationRegistryPayload,
    PlannedIntegrationDefinition,
)
from app.integrations.connectors import ConnectorRequirement, connector_target_scope_metadata, get_connector_definition

REGISTRY_UPDATED_AT = datetime(2026, 5, 7, 0, 0, tzinfo=UTC)


def _metadata_label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def _setup_guide(
        provider: str,
        auth_model: str,
        identity_metadata: tuple[ConnectorRequirement, ...] = (),
) -> ConnectorSetupGuideDefinition:
    fields_by_provider: dict[str, list[ConnectorSetupGuideFieldDefinition]] = {
        "telegram-bot": [
            ConnectorSetupGuideFieldDefinition(
                key="bot_token",
                label="Bot token",
                secret=True,
                description="Paste the Telegram Bot API token from BotFather into OneCLI.",
            ),
            ConnectorSetupGuideFieldDefinition(
                key="webhook_secret_ref",
                label="Webhook secret ref",
                secret=False,
                description=(
                    "Store the secret reference used to verify Telegram production webhooks "
                    "as Agency metadata."
                ),
            ),
        ],
        "discord-bot": [
            ConnectorSetupGuideFieldDefinition(
                key="bot_token",
                label="Bot token",
                secret=True,
                description="Paste the Discord bot token from the application bot settings into OneCLI.",
            ),
            ConnectorSetupGuideFieldDefinition(
                key="webhook_public_key",
                label="Webhook public key",
                secret=False,
                description="Store the Discord application public key as Agency metadata for interaction verification.",
            ),
        ],
        "whatsapp-cloud-api": [
            ConnectorSetupGuideFieldDefinition(
                key="access_token",
                label="Access token",
                secret=True,
                description="Paste the Meta WhatsApp Cloud API access token into OneCLI.",
            ),
            ConnectorSetupGuideFieldDefinition(
                key="phone_number_id",
                label="Phone number id",
                secret=False,
                description="Store the delivery phone number id as Agency metadata.",
            ),
            ConnectorSetupGuideFieldDefinition(
                key="app_secret_ref",
                label="App secret ref",
                secret=False,
                description=(
                    "Store the app secret reference used to verify WhatsApp "
                    "production webhooks as Agency metadata."
                ),
            ),
        ],
        "microsoft-teams": [
            ConnectorSetupGuideFieldDefinition(
                key="webhook_secret_ref",
                label="Webhook secret ref",
                secret=False,
                description=(
                    "Store the Teams webhook secret reference used to verify production "
                    "callbacks as Agency metadata."
                ),
            ),
        ],
    }
    default_secret_label = {
        "api key": "API key",
        "access key": "Access key",
        "access token": "Access token",
        "bot token": "Bot token",
        "oauth": "OAuth token set",
    }.get(auth_model.lower(), "Credential")
    fields = fields_by_provider.get(
        provider,
        [
            ConnectorSetupGuideFieldDefinition(
                key=default_secret_label.lower().replace(" ", "_"),
                label=default_secret_label,
                secret=auth_model.lower() != "public api",
                description=f"Store the {auth_model} credential in OneCLI for this connector.",
            )
        ],
    )
    field_keys = {field.key for field in fields}
    fields = [
        *fields,
        *[
            ConnectorSetupGuideFieldDefinition(
                key=requirement.metadata_key,
                label=_metadata_label(requirement.metadata_key),
                secret=False,
                description=requirement.description,
            )
            for requirement in identity_metadata
            if requirement.metadata_key not in field_keys
        ],
    ]
    notes: list[str] = [
        "Each connector installation gets its own Agency-generated id, so multiple bots or workspaces can coexist under separate OneCLI refs."]
    if provider == "telegram-bot":
        # Telegram can store the bot token in OneCLI, but delivery still has to stay direct
        # because the provider token is embedded in the request path rather than a header.
        notes.append(
            "Store the Telegram bot token in OneCLI, but keep delivery and health checks direct because the token is embedded in the URL path. Agency mirrors the same secret into a runtime secret record at completion time, so OneCLI stays the setup/storage layer for direct transport."
        )
    elif provider == "discord-bot":
        notes.append(
            "Register the Discord interactions webhook endpoint in the Discord Developer Portal and point it at the Agency public URL ending in /integrations/conversations/adapters/discord/webhook."
        )
        notes.append(
            "The Discord application public key and the interactions webhook URL are different values; Agency stores the public key as metadata, but Discord still requires a manual endpoint registration step."
        )
    elif provider in {"whatsapp-cloud-api", "slack-app", "microsoft-teams", "twilio-sms"}:
        notes.append(
            "OneCLI can stay in the setup flow for this connector because the provider token is header-based or otherwise proxy-compatible."
        )
    return ConnectorSetupGuideDefinition(
        storagePath=f"onecli://users/{{agency_user_id}}/{provider}/{{agency_installation_id}}",
        fields=fields,
        options=_setup_guide_options(provider),
        agencyStores=[
            "installation id",
            "provider key",
            "display name",
            "onecli credential ref",
            "non-secret metadata",
            "installation status",
        ],
        completionSignal=(
            "OneCLI completes the Agency installation with only the onecli:// credential ref "
            "and non-secret metadata; Agency then marks the installation active."
        ),
        notes=notes,
    )


def _setup_guide_options(provider: str) -> list[ConnectorSetupGuideOptionDefinition]:
    return []


def _capability_surface(backend_key: str) -> str:
    return "connector"


def _module_capabilities(backend_key: str) -> list[str]:
    return []


def _agency_capability_dependencies(backend_key: str) -> list[str]:
    return []


def _ownership_notes(backend_key: str) -> list[str]:
    return []


@dataclass(slots=True)
class IntegrationsRegistryService:
    """Build registry payloads consumed by frontend integration settings screens."""

    def list_categories(self) -> IntegrationRegistryPayload:
        return IntegrationRegistryPayload(
            categories=[
                IntegrationRegistryCategoryDefinition(
                    id="communications",
                    name="Communications",
                    description="Messaging, chat, and email connectors that can be connected through Agency-owned OneCLI setup sessions.",
                    providers={
                        "Telegram": PlannedIntegrationDefinition(
                            backendKey="telegram-bot",
                            authModel="bot token",
                            summary="Bot API connector for notifications, inbound command handling, and chat delivery workflows.",
                            launchPriority="now",
                            providerAliases=["telegram"],
                        ),
                        "WhatsApp Cloud API": PlannedIntegrationDefinition(
                            backendKey="whatsapp-cloud-api",
                            authModel="access token",
                            summary="Business messaging connector for outbound alerts, support handoff, and approval loops.",
                            launchPriority="now",
                            providerAliases=["whatsapp", "meta-whatsapp"],
                        ),
                        "Discord": PlannedIntegrationDefinition(
                            backendKey="discord-bot",
                            authModel="bot token",
                            summary="Guild, channel, and webhook integration for community operations and agent notifications.",
                            launchPriority="now",
                            providerAliases=["discord"],
                        ),
                        "Slack": PlannedIntegrationDefinition(
                            backendKey="slack-app",
                            authModel="oauth",
                            summary="Workspace messaging and slash-command connector for operations, approvals, and incident response.",
                            launchPriority="next",
                            providerAliases=["slack"],
                        ),
                        "Microsoft Teams": PlannedIntegrationDefinition(
                            backendKey="microsoft-teams",
                            authModel="oauth",
                            summary="Teams messaging and workflow surfaces for enterprise collaboration flows.",
                            launchPriority="next",
                            providerAliases=["teams", "microsoft-teams"],
                        ),
                        "Twilio": PlannedIntegrationDefinition(
                            backendKey="twilio-sms",
                            authModel="api key",
                            summary="SMS and voice delivery primitive for OTP, escalation, and reminder workflows.",
                            launchPriority="next",
                            providerAliases=["twilio"],
                        ),
                        "Gmail": PlannedIntegrationDefinition(
                            backendKey="gmail",
                            authModel="oauth",
                            summary="Mailbox connector for send, reply, triage, and notification digests.",
                            launchPriority="later",
                            providerAliases=["google-mail"],
                        ),
                        "Outlook": PlannedIntegrationDefinition(
                            backendKey="outlook-email",
                            authModel="oauth",
                            summary="Microsoft mail connector for enterprise outbound and inbound automation.",
                            launchPriority="later",
                            providerAliases=["outlook", "microsoft-outlook"],
                        ),
                    },
                ),
                IntegrationRegistryCategoryDefinition(
                    id="productivity",
                    name="Productivity",
                    description="Work management and office-suite connectors available for Agency-owned OneCLI credential setup.",
                    providers={
                        "Notion": PlannedIntegrationDefinition(
                            backendKey="notion",
                            authModel="oauth",
                            summary="Workspace knowledge connector for search, publishing, and structured page updates.",
                            launchPriority="next",
                            providerAliases=["notion"],
                        ),
                        "Linear": PlannedIntegrationDefinition(
                            backendKey="linear",
                            authModel="oauth",
                            summary="Issue and project workflow connector for planning, triage, and release operations.",
                            launchPriority="next",
                            providerAliases=["linear"],
                        ),
                        "Jira": PlannedIntegrationDefinition(
                            backendKey="jira",
                            authModel="oauth",
                            summary="Ticketing connector for enterprise engineering workflows and support queues.",
                            launchPriority="later",
                            providerAliases=["atlassian-jira"],
                        ),
                        "Confluence": PlannedIntegrationDefinition(
                            backendKey="confluence",
                            authModel="oauth",
                            summary="Team knowledge base connector for retrieval, drafting, and documentation sync.",
                            launchPriority="later",
                            providerAliases=["atlassian-confluence"],
                        ),
                        "Airtable": PlannedIntegrationDefinition(
                            backendKey="airtable",
                            authModel="api key",
                            summary="Structured workspace connector for lightweight CRM, ops queues, and table-driven workflows.",
                            launchPriority="later",
                            providerAliases=["airtable"],
                        ),
                        "Google Workspace": PlannedIntegrationDefinition(
                            backendKey="google-workspace",
                            authModel="oauth",
                            summary="Docs, Sheets, Drive, and Calendar family for office productivity flows.",
                            launchPriority="next",
                            providerAliases=["google-drive", "google-calendar", "google-docs", "google-sheets"],
                        ),
                        "Microsoft 365": PlannedIntegrationDefinition(
                            backendKey="microsoft-365",
                            authModel="oauth",
                            summary="Outlook, Calendar, OneDrive, and SharePoint family for enterprise collaboration.",
                            launchPriority="next",
                            providerAliases=["office365", "microsoft-365", "sharepoint", "onedrive"],
                        ),
                    },
                ),
                IntegrationRegistryCategoryDefinition(
                    id="developer",
                    name="Developer",
                    description="Engineering-facing connectors available for Agency-owned OneCLI credential setup.",
                    providers={
                        "GitHub": PlannedIntegrationDefinition(
                            backendKey="github",
                            authModel="oauth",
                            summary="Repository, PR, issue, and CI connector for engineering automation.",
                            launchPriority="next",
                            providerAliases=["github"],
                        ),
                        "GitLab": PlannedIntegrationDefinition(
                            backendKey="gitlab",
                            authModel="oauth",
                            summary="Source control and CI connector for self-hosted or GitLab-native workflows.",
                            launchPriority="later",
                            providerAliases=["gitlab"],
                        ),
                        "Sentry": PlannedIntegrationDefinition(
                            backendKey="sentry",
                            authModel="api key",
                            summary="Incident and error monitoring connector for alert enrichment and triage.",
                            launchPriority="later",
                            providerAliases=["sentry"],
                        ),
                        "PagerDuty": PlannedIntegrationDefinition(
                            backendKey="pagerduty",
                            authModel="api key",
                            summary="On-call and escalation connector for human-in-the-loop operational workflows.",
                            launchPriority="later",
                            providerAliases=["pagerduty"],
                        ),
                    },
                ),
                IntegrationRegistryCategoryDefinition(
                    id="media-creative",
                    name="Media & Creative",
                    description="Creative and publishing connectors available for Agency-owned OneCLI credential setup.",
                    providers={
                        "Figma": PlannedIntegrationDefinition(
                            backendKey="figma",
                            authModel="oauth",
                            summary="Design file connector for implementation context, component retrieval, and review loops.",
                            launchPriority="next",
                            providerAliases=["figma"],
                        ),
                        "Canva": PlannedIntegrationDefinition(
                            backendKey="canva",
                            authModel="oauth",
                            summary="Asset and template connector for social, marketing, and light design automation.",
                            launchPriority="later",
                            providerAliases=["canva"],
                        ),
                        "YouTube": PlannedIntegrationDefinition(
                            backendKey="youtube",
                            authModel="oauth",
                            summary="Channel and content connector for publishing, metadata, and reporting workflows.",
                            launchPriority="later",
                            providerAliases=["google-youtube"],
                        ),
                        "Adobe": PlannedIntegrationDefinition(
                            backendKey="adobe-creative-cloud",
                            authModel="oauth",
                            summary="Creative Cloud family placeholder for asset review and production handoff automation.",
                            launchPriority="later",
                            providerAliases=["adobe", "creative-cloud"],
                        ),
                    },
                ),
                IntegrationRegistryCategoryDefinition(
                    id="search-knowledge",
                    name="Search / Knowledge",
                    description="Retrieval and external knowledge connectors available for Agency-owned OneCLI credential setup.",
                    providers={
                        "Perplexity": PlannedIntegrationDefinition(
                            backendKey="perplexity",
                            authModel="api key",
                            summary="Web answer and research connector for augmented retrieval and citation workflows.",
                            launchPriority="later",
                            providerAliases=["perplexity"],
                        ),
                        "Tavily": PlannedIntegrationDefinition(
                            backendKey="tavily",
                            authModel="api key",
                            summary="Search API connector for controlled web retrieval in agent runs.",
                            launchPriority="later",
                            providerAliases=["tavily"],
                        ),
                        "Wikipedia": PlannedIntegrationDefinition(
                            backendKey="wikipedia",
                            authModel="public api",
                            summary="Reference data connector for lightweight public knowledge retrieval.",
                            launchPriority="later",
                        ),
                    },
                ),
                IntegrationRegistryCategoryDefinition(
                    id="storage",
                    name="Storage",
                    description="File and object-store connectors available for Agency-owned OneCLI credential setup.",
                    providers={
                        "S3": PlannedIntegrationDefinition(
                            backendKey="s3",
                            authModel="access key",
                            summary="Bucket and object storage connector for artifacts, documents, and workflow payload exchange.",
                            launchPriority="next",
                            providerAliases=["aws-s3"],
                        ),
                        "Google Drive": PlannedIntegrationDefinition(
                            backendKey="google-drive",
                            authModel="oauth",
                            summary="Drive connector for document retrieval, writeback, and shared workspace sync.",
                            launchPriority="next",
                            providerAliases=["google-workspace-drive"],
                        ),
                        "Dropbox": PlannedIntegrationDefinition(
                            backendKey="dropbox",
                            authModel="oauth",
                            summary="Cloud file connector for assets, exports, and folder-triggered workflows.",
                            launchPriority="later",
                            providerAliases=["dropbox"],
                        ),
                        "OneDrive": PlannedIntegrationDefinition(
                            backendKey="onedrive",
                            authModel="oauth",
                            summary="Microsoft file storage connector for enterprise document workflows.",
                            launchPriority="later",
                            providerAliases=["microsoft-onedrive"],
                        ),
                        "SharePoint": PlannedIntegrationDefinition(
                            backendKey="sharepoint",
                            authModel="oauth",
                            summary="Document library connector for team knowledge, approvals, and enterprise content flows.",
                            launchPriority="later",
                            providerAliases=["microsoft-sharepoint"],
                        ),
                    },
                ),
            ],
            updated_at=REGISTRY_UPDATED_AT,
        )

    def list_connector_capabilities(self) -> ConnectorCapabilitiesPayload:
        connectors: dict[str, ConnectorCapabilityDefinition] = {}
        for category in self.list_categories().categories:
            for display_name, planned in category.providers.items():
                definition = get_connector_definition(planned.backendKey)
                target_scope_metadata = connector_target_scope_metadata(planned.backendKey)
                connectors[planned.backendKey] = ConnectorCapabilityDefinition(
                    backendKey=planned.backendKey,
                    displayName=display_name,
                    authModel=planned.authModel,
                    providerAliases=list(planned.providerAliases),
                    capabilitySurface=_capability_surface(planned.backendKey),
                    moduleCapabilities=_module_capabilities(planned.backendKey),
                    dependsOnAgencyCapabilities=_agency_capability_dependencies(planned.backendKey),
                    ownershipNotes=_ownership_notes(planned.backendKey),
                    onecliTransportMode="direct" if planned.backendKey in {"telegram-bot", "discord-bot"} else "proxy",
                    healthSupported=bool(definition and definition.health_check is not None),
                    requiredMetadata=[
                        ConnectorMetadataRequirementDefinition(
                            key=requirement.metadata_key,
                            description=requirement.description,
                        )
                        for requirement in (definition.required_metadata if definition else ())
                        if requirement.required_for_credential
                    ],
                    instanceIdentityMetadata=[
                        ConnectorMetadataRequirementDefinition(
                            key=requirement.metadata_key,
                            description=requirement.description,
                        )
                        for requirement in (definition.instance_identity_metadata if definition else ())
                    ],
                    targetScopeMetadata=[
                        ConnectorMetadataRequirementDefinition(
                            key=requirement.metadata_key,
                            description=requirement.description,
                        )
                        for requirement in target_scope_metadata
                    ],
                    supportedSecretRefSchemes=["onecli://", "env://", "env:"],
                    onecliSetupGuide=_setup_guide(
                        planned.backendKey,
                        planned.authModel,
                        definition.instance_identity_metadata if definition else (),
                    ),
                )
        return ConnectorCapabilitiesPayload(connectors=connectors, updated_at=REGISTRY_UPDATED_AT)
