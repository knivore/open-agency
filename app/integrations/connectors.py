"""Static connector registry and metadata validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal


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
    alternative_metadata_keys: tuple[str, ...] = ()
    required_for_credential: bool = True


@dataclass(frozen=True, slots=True)
class ConnectorDefinition:
    key: str
    aliases: tuple[str, ...] = ()
    required_metadata: tuple[ConnectorRequirement, ...] = ()
    instance_identity_metadata: tuple[ConnectorRequirement, ...] = ()
    health_check: ConnectorHealthCheck | None = None
    onecli_transport_mode: Literal["proxy", "direct"] = "proxy"


def _identity(metadata_key: str, description: str) -> ConnectorRequirement:
    return ConnectorRequirement(
        metadata_key=metadata_key,
        description=description,
        required_for_credential=False,
    )


_CONNECTOR_DEFINITIONS: tuple[ConnectorDefinition, ...] = (
    ConnectorDefinition(
        key="telegram-bot",
        aliases=("telegram",),
        instance_identity_metadata=(
            _identity("bot_user_id", "Telegram bot user id for distinguishing multiple bot tokens."),
            _identity("bot_username", "Telegram bot username shown to operators and agents."),
        ),
        required_metadata=(
            ConnectorRequirement(
                metadata_key="webhook_secret_ref",
                alternative_metadata_keys=("webhook_secret_token", "webhook_secret_token_sha256"),
                description=(
                    "Telegram production webhooks require metadata.webhook_secret_ref "
                    "or metadata.webhook_secret_token."
                ),
                required_for_credential=False,
            ),
        ),
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
        instance_identity_metadata=(
            _identity("business_account_id", "Meta WhatsApp Business Account id for this sender."),
            _identity("display_phone_number", "Human-readable sender phone number for this credential."),
        ),
        required_metadata=(
            ConnectorRequirement(
                metadata_key="phone_number_id",
                description="WhatsApp Cloud API credentials require metadata.phone_number_id.",
            ),
            ConnectorRequirement(
                metadata_key="app_secret_ref",
                alternative_metadata_keys=("app_secret",),
                description=(
                    "WhatsApp production webhooks require metadata.app_secret_ref "
                    "or metadata.app_secret."
                ),
                required_for_credential=False,
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
        onecli_transport_mode="direct",
        instance_identity_metadata=(
            _identity("application_id", "Discord application id that owns this bot token."),
            _identity("bot_user_id", "Discord bot user id for distinguishing multiple bot tokens."),
            _identity("default_guild_id", "Default Discord guild id this credential is intended to operate in."),
        ),
        required_metadata=(
            ConnectorRequirement(
                metadata_key="webhook_public_key",
                description="Discord production webhooks require metadata.webhook_public_key.",
                required_for_credential=False,
            ),
        ),
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
    ConnectorDefinition(
        key="slack-app",
        aliases=("slack",),
        instance_identity_metadata=(
            _identity("workspace_id", "Slack workspace/team id for this app installation."),
            _identity("workspace_name", "Slack workspace name shown to operators and agents."),
            _identity("bot_user_id", "Slack bot user id installed in the workspace."),
            _identity(
                "default_channel_id",
                "Default Slack channel id when the workflow has no explicit channel target.",
            ),
        ),
    ),
    ConnectorDefinition(
        key="microsoft-teams",
        aliases=("teams",),
        instance_identity_metadata=(
            _identity("tenant_id", "Microsoft Entra tenant id for this Teams installation."),
            _identity("team_id", "Default Teams team id for this connector instance."),
            _identity("channel_id", "Default Teams channel id for delivery or collaboration."),
        ),
        required_metadata=(
            ConnectorRequirement(
                metadata_key="webhook_secret_ref",
                alternative_metadata_keys=("webhook_secret",),
                description=(
                    "Microsoft Teams production webhooks require metadata.webhook_secret_ref "
                    "or metadata.webhook_secret."
                ),
                required_for_credential=False,
            ),
        ),
    ),
    ConnectorDefinition(
        key="twilio-sms",
        aliases=("twilio",),
        instance_identity_metadata=(
            _identity("account_sid", "Twilio account SID that owns the sender."),
            _identity("from_number", "Twilio sender phone number for SMS delivery."),
            _identity("messaging_service_sid", "Twilio Messaging Service SID when delivery uses a sender pool."),
        ),
    ),
    ConnectorDefinition(
        key="gmail",
        aliases=("google-mail",),
        instance_identity_metadata=(
            _identity("workspace_domain", "Google Workspace domain for this mailbox."),
            _identity("mailbox", "Gmail address or delegated mailbox this credential should use."),
        ),
    ),
    ConnectorDefinition(
        key="outlook-email",
        aliases=("outlook", "microsoft-outlook"),
        instance_identity_metadata=(
            _identity("tenant_id", "Microsoft Entra tenant id for this mailbox."),
            _identity("mailbox", "Outlook address or shared mailbox this credential should use."),
        ),
    ),
    ConnectorDefinition(
        key="notion",
        instance_identity_metadata=(
            _identity("workspace_id", "Notion workspace id connected by this OAuth/token."),
            _identity("workspace_name", "Notion workspace name shown to operators and agents."),
        ),
    ),
    ConnectorDefinition(
        key="linear",
        instance_identity_metadata=(
            _identity("workspace_id", "Linear workspace id for this connector instance."),
            _identity("team_id", "Default Linear team id for issue routing."),
        ),
    ),
    ConnectorDefinition(
        key="jira",
        aliases=("atlassian-jira",),
        instance_identity_metadata=(
            _identity("site_id", "Atlassian site/cloud id for this Jira connector."),
            _identity("project_key", "Default Jira project key for issue creation and lookup."),
        ),
    ),
    ConnectorDefinition(
        key="confluence",
        aliases=("atlassian-confluence",),
        instance_identity_metadata=(
            _identity("site_id", "Atlassian site/cloud id for this Confluence connector."),
            _identity("space_key", "Default Confluence space key for pages and search."),
        ),
    ),
    ConnectorDefinition(
        key="airtable",
        instance_identity_metadata=(
            _identity("workspace_id", "Airtable workspace id for this credential."),
            _identity("base_id", "Default Airtable base id for records and automation."),
        ),
    ),
    ConnectorDefinition(
        key="google-workspace",
        aliases=("google-drive", "google-calendar", "google-docs", "google-sheets"),
        instance_identity_metadata=(
            _identity("workspace_domain", "Google Workspace domain for this connector."),
            _identity("admin_customer_id", "Google Workspace customer id when using admin APIs."),
            _identity("default_calendar_id", "Default calendar id for scheduling workflows."),
        ),
    ),
    ConnectorDefinition(
        key="microsoft-365",
        aliases=("office365", "sharepoint", "onedrive"),
        instance_identity_metadata=(
            _identity("tenant_id", "Microsoft Entra tenant id for this connector."),
            _identity("site_id", "Default SharePoint site id for document workflows."),
            _identity("drive_id", "Default OneDrive/SharePoint drive id for file workflows."),
        ),
    ),
    ConnectorDefinition(
        key="github",
        instance_identity_metadata=(
            _identity("owner", "GitHub organization or user owner for this credential."),
            _identity("repo", "Default GitHub repository name or owner/repo scope."),
            _identity("installation_id", "GitHub App installation id when using app credentials."),
        ),
    ),
    ConnectorDefinition(
        key="gitlab",
        instance_identity_metadata=(
            _identity("namespace", "GitLab group/user namespace for this credential."),
            _identity("project_id", "Default GitLab project id for issue, MR, and repository operations."),
        ),
    ),
    ConnectorDefinition(
        key="sentry",
        instance_identity_metadata=(
            _identity("organization_slug", "Sentry organization slug for this connector."),
            _identity("project_slug", "Default Sentry project slug for event and issue workflows."),
        ),
    ),
    ConnectorDefinition(
        key="pagerduty",
        instance_identity_metadata=(
            _identity("account_subdomain", "PagerDuty account subdomain for this connector."),
            _identity("service_id", "Default PagerDuty service id for incidents."),
            _identity("escalation_policy_id", "Default escalation policy id for routing."),
        ),
    ),
    ConnectorDefinition(
        key="figma",
        instance_identity_metadata=(
            _identity("team_id", "Figma team id for this credential."),
            _identity("project_id", "Default Figma project id for files and comments."),
        ),
    ),
    ConnectorDefinition(
        key="canva",
        instance_identity_metadata=(
            _identity("team_id", "Canva team id for this connector."),
            _identity("brand_folder_id", "Default Canva brand/folder id for design workflows."),
        ),
    ),
    ConnectorDefinition(
        key="youtube",
        aliases=("google-youtube",),
        instance_identity_metadata=(
            _identity("channel_id", "YouTube channel id this credential operates on."),
            _identity("brand_account_id", "Google brand account id when the channel is brand-owned."),
        ),
    ),
    ConnectorDefinition(
        key="adobe-creative-cloud",
        aliases=("adobe", "creative-cloud"),
        instance_identity_metadata=(
            _identity("organization_id", "Adobe organization id for this connector."),
            _identity("project_id", "Default Adobe project/library id for creative workflows."),
        ),
    ),
    ConnectorDefinition(
        key="perplexity",
        instance_identity_metadata=(
            _identity("account_id", "Perplexity account or workspace id for this API key."),
            _identity("allowed_domains", "Comma-separated domain allowlist for research workflows."),
        ),
    ),
    ConnectorDefinition(
        key="tavily",
        instance_identity_metadata=(
            _identity("account_id", "Tavily account or workspace id for this API key."),
            _identity("allowed_domains", "Comma-separated domain allowlist for search workflows."),
        ),
    ),
    ConnectorDefinition(key="wikipedia"),
    ConnectorDefinition(
        key="s3",
        aliases=("aws-s3",),
        instance_identity_metadata=(
            _identity("aws_account_id", "AWS account id that owns the bucket."),
            _identity("region", "AWS region for this S3 connector."),
            _identity("bucket", "Default S3 bucket name."),
            _identity("prefix", "Default S3 key prefix for workflow files."),
        ),
    ),
    ConnectorDefinition(
        key="google-drive",
        aliases=("google-workspace-drive",),
        instance_identity_metadata=(
            _identity("workspace_domain", "Google Workspace domain for this Drive connector."),
            _identity("drive_id", "Shared drive id, or 'my-drive' for a user drive."),
            _identity("folder_id", "Default Google Drive folder id for files."),
        ),
    ),
    ConnectorDefinition(
        key="dropbox",
        instance_identity_metadata=(
            _identity("team_id", "Dropbox team id for team-owned credentials."),
            _identity("namespace_id", "Dropbox namespace id for the selected account/team space."),
            _identity("folder_path", "Default Dropbox folder path for workflow files."),
        ),
    ),
    ConnectorDefinition(
        key="onedrive",
        aliases=("microsoft-onedrive",),
        instance_identity_metadata=(
            _identity("tenant_id", "Microsoft Entra tenant id for this OneDrive connector."),
            _identity("drive_id", "Default OneDrive drive id."),
            _identity("folder_id", "Default OneDrive folder id for files."),
        ),
    ),
    ConnectorDefinition(
        key="sharepoint",
        aliases=("microsoft-sharepoint",),
        instance_identity_metadata=(
            _identity("tenant_id", "Microsoft Entra tenant id for this SharePoint connector."),
            _identity("site_id", "SharePoint site id for this connector."),
            _identity("drive_id", "Document library drive id for file workflows."),
        ),
    ),
)

_TARGET_SCOPE_METADATA: dict[str, tuple[ConnectorRequirement, ...]] = {
    "telegram-bot": (
        _identity("chat_id", "Telegram chat id used as the workflow delivery or command target."),
    ),
    "discord-bot": (
        _identity("guild_id", "Discord guild id for the workflow target."),
        _identity("channel_id", "Discord channel id used as the workflow delivery or command target."),
    ),
    "whatsapp-cloud-api": (
        _identity("recipient_phone", "Recipient phone number or WhatsApp wa_id for workflow delivery."),
    ),
    "slack-app": (
        _identity("workspace_id", "Slack workspace/team id for the workflow target."),
        _identity("channel_id", "Slack channel id for messages, approvals, or commands."),
    ),
    "microsoft-teams": (
        _identity("team_id", "Teams team id for the workflow target."),
        _identity("channel_id", "Teams channel id for messages, approvals, or commands."),
    ),
    "twilio-sms": (
        _identity("recipient_phone", "SMS recipient phone number or allowlisted recipient group."),
    ),
    "gmail": (
        _identity("mailbox", "Gmail mailbox or delegated mailbox used by the workflow."),
        _identity("recipient_allowlist", "Comma-separated recipient allowlist for outbound email."),
    ),
    "outlook-email": (
        _identity("mailbox", "Outlook mailbox or shared mailbox used by the workflow."),
        _identity("recipient_allowlist", "Comma-separated recipient allowlist for outbound email."),
    ),
    "notion": (
        _identity("database_id", "Default Notion database id for records or search."),
        _identity("page_id", "Default Notion page id for publishing or updates."),
    ),
    "linear": (
        _identity("team_id", "Linear team id for issue routing."),
        _identity("project_id", "Linear project id for planning workflows."),
    ),
    "jira": (
        _identity("project_key", "Jira project key for issue creation and lookup."),
    ),
    "confluence": (
        _identity("space_key", "Confluence space key for pages and search."),
    ),
    "airtable": (
        _identity("base_id", "Airtable base id for the workflow target."),
        _identity("table_id", "Airtable table id or name for record operations."),
    ),
    "google-workspace": (
        _identity("calendar_id", "Google Calendar id for scheduling workflows."),
        _identity("drive_id", "Google Drive/shared drive id for document workflows."),
    ),
    "microsoft-365": (
        _identity("site_id", "SharePoint site id for document workflows."),
        _identity("drive_id", "OneDrive/SharePoint drive id for file workflows."),
    ),
    "github": (
        _identity("owner", "GitHub organization or user owner."),
        _identity("repo", "GitHub repository name or owner/repo target."),
        _identity("branch", "Default branch or branch allowlist for write operations."),
    ),
    "gitlab": (
        _identity("project_id", "GitLab project id for issue, MR, and repository operations."),
        _identity("branch", "Default branch or branch allowlist for write operations."),
    ),
    "sentry": (
        _identity("organization_slug", "Sentry organization slug."),
        _identity("project_slug", "Sentry project slug for events and issues."),
    ),
    "pagerduty": (
        _identity("service_id", "PagerDuty service id for incidents."),
        _identity("escalation_policy_id", "PagerDuty escalation policy id for routing."),
    ),
    "figma": (
        _identity("file_key", "Figma file key for design context and comments."),
        _identity("project_id", "Figma project id for file discovery."),
    ),
    "canva": (
        _identity("design_id", "Canva design id for publishing or review."),
        _identity("folder_id", "Canva folder id for asset workflows."),
    ),
    "youtube": (
        _identity("channel_id", "YouTube channel id for publishing workflows."),
        _identity("playlist_id", "Optional playlist id for video organization."),
    ),
    "adobe-creative-cloud": (
        _identity("project_id", "Adobe project/library id for creative workflows."),
        _identity("asset_id", "Adobe asset id for review or production handoff."),
    ),
    "perplexity": (
        _identity("allowed_domains", "Domain allowlist for research queries."),
    ),
    "tavily": (
        _identity("allowed_domains", "Domain allowlist for search queries."),
    ),
    "s3": (
        _identity("bucket", "S3 bucket name for workflow files."),
        _identity("region", "AWS region for the S3 bucket."),
        _identity("prefix", "S3 key prefix for workflow read/write operations."),
    ),
    "google-drive": (
        _identity("drive_id", "Google Drive/shared drive id."),
        _identity("folder_id", "Google Drive folder id for files."),
    ),
    "dropbox": (
        _identity("namespace_id", "Dropbox namespace id for the selected account/team space."),
        _identity("folder_path", "Dropbox folder path for workflow files."),
    ),
    "onedrive": (
        _identity("drive_id", "OneDrive drive id."),
        _identity("folder_id", "OneDrive folder id for files."),
    ),
    "sharepoint": (
        _identity("site_id", "SharePoint site id."),
        _identity("drive_id", "SharePoint document library drive id."),
    ),
}


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


def connector_target_scope_metadata(provider: str | None) -> tuple[ConnectorRequirement, ...]:
    normalized = normalize_connector_provider_key(provider)
    if normalized is None:
        return ()
    return _TARGET_SCOPE_METADATA.get(normalized, ())


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
        if not requirement.required_for_credential:
            continue
        keys = (requirement.metadata_key, *requirement.alternative_metadata_keys)
        values = [metadata.get(key) for key in keys]
        if not any(isinstance(value, str) and value.strip() for value in values):
            errors.append(requirement.description)
    return errors


def connector_health_supported(provider: str | None) -> bool:
    definition = get_connector_definition(provider)
    return definition is not None and definition.health_check is not None
