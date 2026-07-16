"""Read-only OneCLI metadata verification for connector setup completion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import get_settings
from app.integrations.onecli_catalog import OneCLISecretProfile
from app.integrations.secrets import resolve_secret_ref


class OneCLIControlError(ValueError):
    """A safe, user-facing OneCLI verification failure."""


@dataclass(frozen=True, slots=True)
class VerifiedOneCLIResource:
    id: str
    kind: str


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(slots=True)
class OneCLIControlClient:
    api_url: str
    api_key: str
    transport: httpx.AsyncBaseTransport | None = None

    @classmethod
    def from_settings(cls) -> "OneCLIControlClient":
        settings = get_settings()
        if settings.onecli_multi_user_mode:
            raise OneCLIControlError(
                "Verified OneCLI setup needs an owner-specific control key in multi-user mode. "
                "Per-owner control-key mapping is not configured."
            )
        secret_ref = str(settings.onecli_control_api_key_secret_ref or "").strip()
        if not secret_ref:
            raise OneCLIControlError(
                "OneCLI verification is not configured. Set ONECLI_CONTROL_API_KEY_SECRET_REF "
                "to an environment-backed OneCLI project API key."
            )
        resolved = resolve_secret_ref(secret_ref)
        api_key = str(resolved.value or "").strip()
        if not api_key:
            raise OneCLIControlError(resolved.error or "The OneCLI control API key could not be resolved.")
        if not api_key.startswith("oc_"):
            raise OneCLIControlError("The configured OneCLI control API key is not a valid oc_ key.")
        return cls(api_url=settings.onecli_api_url.rstrip("/"), api_key=api_key)

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(
            timeout=10.0,
            trust_env=False,
            transport=self.transport,
        ) as client:
            try:
                response = await client.get(f"{self.api_url}{path}", headers=headers, params=params)
            except httpx.HTTPError as exc:
                raise OneCLIControlError("Agency could not reach the OneCLI metadata API.") from exc

        if response.status_code == 401:
            raise OneCLIControlError("OneCLI rejected the configured control API key.")
        if response.status_code == 404:
            raise OneCLIControlError(
                "The installed OneCLI does not expose the v1 metadata API. Upgrade OneCLI to v1.41.0 or newer."
            )
        if response.status_code != 200:
            raise OneCLIControlError(
                f"OneCLI metadata verification returned HTTP {response.status_code}."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OneCLIControlError("OneCLI returned an invalid metadata response.") from exc
        if not isinstance(payload, list):
            raise OneCLIControlError("OneCLI returned an unexpected metadata response.")
        return [item for item in payload if isinstance(item, dict)]

    @staticmethod
    def _created_during_session(item: dict[str, Any], field: str, started_at: datetime) -> bool:
        created_at = _parse_timestamp(item.get(field))
        if created_at is None:
            return False
        # Agency and OneCLI usually share a host, but tolerate small clock skew
        # without allowing an old credential to satisfy a new setup session.
        return created_at >= started_at - timedelta(minutes=2)

    @staticmethod
    def _matches_profile(item: dict[str, Any], profile: OneCLISecretProfile | None) -> bool:
        if profile is None:
            return True
        if str(item.get("hostPattern") or "").lower() != profile.host_pattern.lower():
            return False
        stored_path = item.get("pathPattern") or None
        if stored_path != profile.path_pattern:
            return False
        config = item.get("injectionConfig")
        if not isinstance(config, dict):
            return False
        if profile.injection_target == "header":
            return (
                config.get("headerName") == profile.header_name
                and (config.get("valueFormat") or "{value}") == (profile.value_format or "{value}")
            )
        if profile.injection_target == "url_parameter":
            return (
                config.get("paramName") == profile.parameter_name
                and (config.get("paramFormat") or "{value}") == (profile.parameter_format or "{value}")
            )
        return config.get("pathTemplate") == profile.path_template

    async def verify_secret(
        self,
        *,
        resource_name: str,
        started_at: datetime,
        profile: OneCLISecretProfile | None,
    ) -> VerifiedOneCLIResource:
        secrets = await self._get("/v1/secrets")
        matches = [
            item
            for item in secrets
            if item.get("type") == "generic"
            and item.get("name") == resource_name
            and self._created_during_session(item, "createdAt", started_at)
            and self._matches_profile(item, profile)
        ]
        if not matches:
            raise OneCLIControlError(
                "No matching OneCLI secret was found for this setup session. Save the prefilled "
                "secret in OneCLI, including its host, path, and injection settings, then try again."
            )
        resource_id = str(matches[0].get("id") or "").strip()
        if not resource_id:
            raise OneCLIControlError("The matching OneCLI secret did not include a resource id.")
        return VerifiedOneCLIResource(id=resource_id, kind="secrets")

    async def verify_connection(
        self,
        *,
        provider: str,
        started_at: datetime,
    ) -> VerifiedOneCLIResource:
        connections = await self._get("/v1/connections", params={"provider": provider})
        matches = [
            item
            for item in connections
            if item.get("provider") == provider
            and item.get("status") == "connected"
            and self._created_during_session(item, "connectedAt", started_at)
        ]
        if not matches:
            raise OneCLIControlError(
                "No newly connected OneCLI account was found for this setup session. Complete or "
                "reconnect the provider in OneCLI, then try again."
            )
        resource_id = str(matches[0].get("id") or "").strip()
        if not resource_id:
            raise OneCLIControlError("The matching OneCLI connection did not include a resource id.")
        return VerifiedOneCLIResource(id=resource_id, kind="connections")
