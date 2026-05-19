from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain import (
    ConnectorCapabilitiesPayload,
    ConnectorCapabilityDefinition,
    ConnectorMetadataRequirementDefinition,
    IntegrationRegistryCategoryDefinition,
    IntegrationRegistryPayload,
    PlannedIntegrationDefinition,
)
from app.integrations import get_connector_definition

REGISTRY_UPDATED_AT = datetime(2026, 5, 7, 0, 0, tzinfo=UTC)


@dataclass(slots=True)
class IntegrationsRegistryService:
    def list_categories(self) -> IntegrationRegistryPayload:
        return IntegrationRegistryPayload(
            categories=[
                IntegrationRegistryCategoryDefinition(
                    id="communications",
                    name="Communications",
                    description="Messaging, chat, and email connectors staged while connector-specific CRUD and health flows are rolled out.",
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
                    description="Work management and office-suite connectors that need backend route groups and credential lifecycle support.",
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
                    description="Engineering-facing connectors planned after the backend exposes credentialed provider route groups beyond tools and MCP servers.",
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
                    description="Creative and publishing connectors that remain backend-visible as planned metadata until dedicated connector operations exist.",
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
                    description="Retrieval and external knowledge connectors planned outside the current tool and MCP server route groups.",
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
                    description="File and object-store connectors that need backend credential and file-operation routes before they become configurable.",
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
                connectors[planned.backendKey] = ConnectorCapabilityDefinition(
                    backendKey=planned.backendKey,
                    displayName=display_name,
                    authModel=planned.authModel,
                    providerAliases=list(planned.providerAliases),
                    healthSupported=bool(definition and definition.health_check is not None),
                    requiredMetadata=[
                        ConnectorMetadataRequirementDefinition(
                            key=requirement.metadata_key,
                            description=requirement.description,
                        )
                        for requirement in (definition.required_metadata if definition else ())
                    ],
                    supportedSecretRefSchemes=["env://", "env:"],
                )
        return ConnectorCapabilitiesPayload(connectors=connectors, updated_at=REGISTRY_UPDATED_AT)
