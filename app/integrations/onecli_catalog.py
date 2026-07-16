"""OneCLI resource contracts shared by setup, verification, and the frontend registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


OneCLIInjectionTarget = Literal["header", "url_parameter", "url_path"]


@dataclass(frozen=True, slots=True)
class OneCLISecretProfile:
    host_pattern: str
    path_pattern: str | None = None
    injection_target: OneCLIInjectionTarget = "header"
    header_name: str | None = None
    value_format: str | None = None
    parameter_name: str | None = None
    parameter_format: str | None = None
    path_template: str | None = None


# These ids are verified against OneCLI v1.41.0. Keeping the mapping in the
# backend makes setup verification and frontend navigation use one contract.
ONECLI_NATIVE_APP_BY_CONNECTOR: dict[str, str] = {
    "cloudflare": "cloudflare",
    "confluence": "confluence",
    "dropbox": "dropbox",
    "gmail": "gmail",
    "github": "github",
    "gitlab": "gitlab",
    "google-drive": "google-drive",
    "google-workspace": "google-admin",
    "jira": "jira",
    "monday": "monday",
    "notion": "notion",
    "s3": "aws",
    "youtube": "youtube",
}


ONECLI_SECRET_PROFILE_BY_CONNECTOR: dict[str, OneCLISecretProfile] = {
    "airtable": OneCLISecretProfile(
        host_pattern="api.airtable.com",
        path_pattern="/v0/*",
        header_name="Authorization",
        value_format="Bearer {value}",
    ),
    "discord-bot": OneCLISecretProfile(
        host_pattern="discord.com",
        path_pattern="/api/v10/*",
        header_name="Authorization",
        value_format="Bot {value}",
    ),
    "figma": OneCLISecretProfile(
        host_pattern="api.figma.com",
        path_pattern="/v1/*",
        header_name="X-Figma-Token",
        value_format="{value}",
    ),
    "linear": OneCLISecretProfile(
        host_pattern="api.linear.app",
        path_pattern="/graphql",
        header_name="Authorization",
        value_format="{value}",
    ),
    "pagerduty": OneCLISecretProfile(
        host_pattern="*.pagerduty.com",
        path_pattern="/*",
        header_name="Authorization",
        value_format="Token token={value}",
    ),
    "perplexity": OneCLISecretProfile(
        host_pattern="api.perplexity.ai",
        path_pattern="/*",
        header_name="Authorization",
        value_format="Bearer {value}",
    ),
    "sentry": OneCLISecretProfile(
        host_pattern="sentry.io",
        path_pattern="/api/0/*",
        header_name="Authorization",
        value_format="Bearer {value}",
    ),
    "slack-app": OneCLISecretProfile(
        host_pattern="slack.com",
        path_pattern="/api/*",
        header_name="Authorization",
        value_format="Bearer {value}",
    ),
    "tavily": OneCLISecretProfile(
        host_pattern="api.tavily.com",
        path_pattern="/*",
        header_name="Authorization",
        value_format="Bearer {value}",
    ),
    "telegram-bot": OneCLISecretProfile(
        host_pattern="api.telegram.org",
        path_pattern="/bot*",
        injection_target="url_path",
        path_template="/bot{value}",
    ),
    "whatsapp-cloud-api": OneCLISecretProfile(
        host_pattern="graph.facebook.com",
        path_pattern="/*",
        header_name="Authorization",
        value_format="Bearer {value}",
    ),
}


def onecli_resource_name(provider: str, setup_session_id: str) -> str:
    """Return a session-unique name so an older secret cannot satisfy a new setup."""

    compact_session_id = setup_session_id.replace("-", "")[:12].lower()
    return f"agency-{provider}-{compact_session_id}"
