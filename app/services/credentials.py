from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

from pydantic import ValidationError

from app.api.context import ApiContext
from app.core.config import get_settings
from app.domain import ConnectorCredentialValidationPayload, CredentialDefinition, CredentialStatus
from app.integrations.connectors import get_connector_definition, normalize_connector_provider_key
from app.integrations.secrets import (
    SecretResolutionResult,
    is_onecli_secret_ref,
    onecli_secret_identifier,
    resolve_secret_ref,
)
from app.services.integrations_registry import IntegrationsRegistryService
from app.services.onecli import OneCLIIdentityMappingService
from app.services.runtime_secrets import open_runtime_secret

RAW_SECRET_PAYLOAD_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "password",
    "raw_secret",
    "refresh_token",
    "secret",
    "token",
    "value",
}
SUPPORTED_SECRET_REF_PREFIXES = ("env://", "env:", "onecli://", "secret://")
CONNECTOR_IDENTITY_SUMMARY_KEYS = (
    "workspace_id",
    "workspace_name",
    "tenant_id",
    "team_id",
    "channel_id",
    "default_channel_id",
    "guild_id",
    "default_guild_id",
    "bot_user_id",
    "bot_username",
    "phone_number_id",
    "business_account_id",
    "display_phone_number",
    "mailbox",
    "site_id",
    "project_key",
    "space_key",
    "base_id",
    "owner",
    "repo",
    "installation_id",
    "namespace",
    "project_id",
    "organization_slug",
    "project_slug",
    "service_id",
    "bucket",
    "region",
    "prefix",
    "drive_id",
    "folder_id",
)


@dataclass(slots=True)
class CredentialService:
    context: ApiContext

    def _connector_capability(self, provider_key: str):
        canonical = normalize_connector_provider_key(provider_key)
        capabilities = IntegrationsRegistryService().list_connector_capabilities()
        return canonical, capabilities.connectors.get(canonical or "")

    def _serializable_connector_validation_errors(self, exc: ValidationError) -> list[str]:
        messages: list[str] = []
        for item in exc.errors():
            message = item.get("msg")
            if isinstance(message, str):
                messages.append(message)
        return messages or ["Invalid connector credential payload"]

    def raw_secret_payload_errors(self, payload: dict[str, Any]) -> list[str]:
        def contains_raw_secret(item: Any) -> bool:
            if isinstance(item, dict):
                for key, value in item.items():
                    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower().replace("-", "_")
                    if normalized in RAW_SECRET_PAYLOAD_KEYS or contains_raw_secret(value):
                        return True
            elif isinstance(item, list):
                return any(contains_raw_secret(value) for value in item)
            return False

        if not contains_raw_secret(payload):
            return []
        return ["Raw secret material must be stored in the secret store and referenced by secret_ref"]

    def secret_ref_format_errors(self, secret_ref: Any) -> list[str]:
        if not isinstance(secret_ref, str) or not secret_ref.strip():
            return ["secret_ref must be a non-empty secret-store reference."]
        normalized = secret_ref.strip()
        prefix = next((item for item in SUPPORTED_SECRET_REF_PREFIXES if normalized.startswith(item)), None)
        if prefix is None:
            return ["secret_ref must use a supported reference scheme."]
        identifier = normalized.removeprefix(prefix).strip()
        if not identifier or any(char.isspace() for char in identifier) or ".." in identifier.split("/"):
            return ["secret_ref must contain a clean non-empty identifier."]
        return []

    def onecli_secret_ref_errors(self, secret_ref: Any, owner_user_id: str) -> list[str]:
        if not is_onecli_secret_ref(secret_ref):
            return []

        identifier = onecli_secret_identifier(secret_ref)
        if not identifier:
            return ["OneCLI credential ref is empty."]
        if any(char.isspace() for char in identifier) or ".." in identifier.split("/"):
            return ["OneCLI credential ref must be a clean path without whitespace or traversal segments."]

        owner_prefix = f"users/{owner_user_id}/"
        if not identifier.startswith(owner_prefix):
            return [f"OneCLI credential refs must be scoped under onecli://{owner_prefix}."]
        return []

    def env_secret_ref_errors(self, secret_ref: Any) -> list[str]:
        if not isinstance(secret_ref, str):
            return []
        normalized = secret_ref.strip()
        if normalized.startswith("env://"):
            variable = normalized.removeprefix("env://").strip()
        elif normalized.startswith("env:"):
            variable = normalized.removeprefix("env:").strip()
        else:
            return []
        if not variable:
            return ["Environment secret ref is empty."]
        settings = get_settings()
        if settings.app_env == "test":
            return []
        allowed = settings.parsed_agency_credential_env_allowlist
        if variable and (variable in allowed or "*" in allowed):
            return []
        return ["Environment secret ref is not allowlisted for user-managed credentials."]

    def credential_payload_errors(self, payload: dict[str, Any], owner_user_id: str) -> list[str]:
        errors = self.raw_secret_payload_errors(payload)
        if "secret_ref" in payload:
            secret_ref = payload.get("secret_ref")
            errors.extend(self.secret_ref_format_errors(secret_ref))
            errors.extend(self.onecli_secret_ref_errors(secret_ref, owner_user_id))
            errors.extend(self.env_secret_ref_errors(secret_ref))
        return errors

    async def _installation_secret_ref_errors(
            self,
            secret_ref: Any,
            owner_user_id: str,
            provider: str | None,
    ) -> list[str]:
        if not isinstance(secret_ref, str) or not secret_ref.startswith("secret://agency/installations/"):
            return []
        installation_id = secret_ref.removeprefix("secret://agency/installations/").strip()
        installation = await self.context.connector_installation_repo.get(installation_id) if installation_id else None
        expected_provider = normalize_connector_provider_key(provider)
        actual_provider = normalize_connector_provider_key(installation.provider) if installation is not None else None
        if (
                installation is None
                or installation.owner_user_id != owner_user_id
                or expected_provider is None
                or actual_provider != expected_provider
        ):
            # Keep missing, cross-owner, and cross-provider references indistinguishable.
            return ["Connector installation secret ref is unavailable for this credential."]
        return []

    async def _raise_for_credential_payload_errors(
            self,
            payload: dict[str, Any],
            owner_user_id: str,
            *,
            provider: str | None = None,
    ) -> None:
        errors = self.credential_payload_errors(payload, owner_user_id)
        errors.extend(
            await self._installation_secret_ref_errors(
                payload.get("secret_ref"),
                owner_user_id,
                provider or payload.get("provider"),
            )
        )
        if errors:
            raise ValueError(errors[0])

    def resolve_connector_capability(self, provider_key: str):
        canonical, capability = self._connector_capability(provider_key)
        if canonical is None or get_connector_definition(canonical) is None or capability is None:
            return None, None
        return canonical, capability

    async def validate_connector_payload(
            self,
            *,
            provider_key: str,
            payload: dict[str, Any],
            owner_user_id: str,
    ) -> ConnectorCredentialValidationPayload | None:
        canonical, capability = self.resolve_connector_capability(provider_key)
        if canonical is None or capability is None:
            return None

        merged = {**payload, "owner_user_id": owner_user_id, "provider": canonical}
        errors = self.credential_payload_errors(payload, owner_user_id)
        errors.extend(await self._installation_secret_ref_errors(payload.get("secret_ref"), owner_user_id, canonical))
        if not errors:
            try:
                CredentialDefinition.model_validate(merged)
            except ValidationError as exc:
                errors = self._serializable_connector_validation_errors(exc)

        return ConnectorCredentialValidationPayload(
            provider=canonical,
            valid=not errors,
            errors=errors,
            capability=capability,
        )

    async def create_credential(self, *, payload: dict[str, Any], owner_user_id: str) -> CredentialDefinition:
        await self._raise_for_credential_payload_errors(payload, owner_user_id)
        merged = {**payload, "owner_user_id": owner_user_id}
        return await self.context.credential_repo.create(CredentialDefinition.model_validate(merged))

    async def create_connector_credential(
            self,
            *,
            provider_key: str,
            payload: dict[str, Any],
            owner_user_id: str,
    ) -> CredentialDefinition | None:
        canonical, capability = self.resolve_connector_capability(provider_key)
        if canonical is None or capability is None:
            return None
        await self._raise_for_credential_payload_errors(payload, owner_user_id, provider=canonical)
        merged = {**payload, "owner_user_id": owner_user_id, "provider": canonical}
        return await self.context.credential_repo.create(CredentialDefinition.model_validate(merged))

    async def list_credentials_for_owner(self, owner_user_id: str) -> list[CredentialDefinition]:
        if hasattr(self.context.credential_repo, "list_by_owner"):
            return await self.context.credential_repo.list_by_owner(owner_user_id)
        return [
            item
            for item in await self.context.credential_repo.list()
            if item.owner_user_id == owner_user_id
        ]

    async def resolve_credential_secret(self, credential: CredentialDefinition):
        secret_ref = credential.secret_ref.strip() if isinstance(credential.secret_ref, str) else ""
        if not secret_ref:
            return resolve_secret_ref(secret_ref)

        if secret_ref.startswith("secret://agency/installations/"):
            installation_id = secret_ref.removeprefix("secret://agency/installations/").strip()
            if not installation_id:
                return resolve_secret_ref(secret_ref)
            installation = await self.context.connector_installation_repo.get(installation_id)
            if installation is None:
                return resolve_secret_ref(secret_ref)
            if (
                    installation.owner_user_id != credential.owner_user_id
                    or normalize_connector_provider_key(installation.provider)
                    != normalize_connector_provider_key(credential.provider)
            ):
                return SecretResolutionResult(
                    value=None,
                    source="agency",
                    identifier=installation_id,
                    error="Connector installation secret ref is unavailable for this credential.",
                )
            sealed_value = installation.runtime_secret_encrypted
            if not sealed_value:
                return resolve_secret_ref(secret_ref)
            try:
                value = open_runtime_secret(sealed_value)
            except ValueError as exc:
                return resolve_secret_ref(secret_ref)
            if value is None:
                return resolve_secret_ref(secret_ref)
            return SecretResolutionResult(value=value, source="agency", identifier=installation_id)

        return resolve_secret_ref(secret_ref)

    def redact_connector_payload(self, value: Any) -> Any:
        redaction_keys = {
            "api_key",
            "apikey",
            "authorization",
            "bearer",
            "client_secret",
            "password",
            "secret",
            "token",
        }

        def redact(item: Any, *, key: str | None = None) -> Any:
            if key and any(rule in key for rule in redaction_keys):
                return "[REDACTED]"
            if isinstance(item, dict):
                return {
                    nested_key: redact(nested_value, key=str(nested_key).lower())
                    for nested_key, nested_value in item.items()
                }
            if isinstance(item, list):
                return [redact(entry, key=key) for entry in item]
            return item

        return redact(value)

    def credential_api_payload(self, credential: CredentialDefinition) -> dict[str, Any]:
        """Expose credential identity and lifecycle without disclosing secret-store locators."""
        payload = credential.model_dump(mode="json", exclude={"secret_ref"})
        secret_ref = credential.secret_ref.strip() if isinstance(credential.secret_ref, str) else ""
        secret_scheme = secret_ref.split(":", 1)[0].lower() if ":" in secret_ref else None
        payload["secret_ref_configured"] = bool(secret_ref)
        payload["secret_ref_scheme"] = secret_scheme
        payload["metadata"] = self.redact_connector_payload(payload.get("metadata", {}))
        payload["rotation_policy"] = self.redact_connector_payload(payload.get("rotation_policy", {}))
        return payload

    def connector_identity_summary(self, credential: CredentialDefinition) -> str | None:
        parts: list[str] = []
        for key in CONNECTOR_IDENTITY_SUMMARY_KEYS:
            value = credential.metadata.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(f"{key}: {value.strip()}")
            elif isinstance(value, int | float | bool):
                parts.append(f"{key}: {value}")
        return " | ".join(parts[:4]) or None

    def connector_credential_summary(self, credential: CredentialDefinition) -> dict[str, Any]:
        status = credential.status.value if hasattr(credential.status, "value") else str(credential.status)
        secret_ref = credential.secret_ref if isinstance(credential.secret_ref, str) else ""
        secret_scheme = secret_ref.split("://", 1)[0] if "://" in secret_ref else secret_ref.split(":", 1)[0]
        return {
            "id": credential.id,
            "name": credential.name,
            "provider": credential.provider,
            "status": status,
            "identity_summary": self.connector_identity_summary(credential),
            "last_rotated_at": credential.last_rotated_at.isoformat() if credential.last_rotated_at else None,
            "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
            "revoked_at": credential.revoked_at.isoformat() if credential.revoked_at else None,
            "secret_version": credential.secret_version,
            "secret_ref_present": bool(secret_ref),
            "secret_ref_scheme": secret_scheme or None,
            "rotation_policy": self.redact_connector_payload(credential.rotation_policy),
            "metadata": self.redact_connector_payload(credential.metadata),
        }

    def _connector_filter_matches(self, actual: Any, expected: Any) -> bool:
        if expected is None:
            return True
        if isinstance(expected, str):
            expected = expected.strip()
            if not expected:
                return True
        if isinstance(actual, str):
            actual_text = actual.strip().lower()
            if isinstance(expected, str):
                return actual_text == expected.strip().lower()
            return actual_text == str(expected).strip().lower()
        if isinstance(actual, list):
            return any(self._connector_filter_matches(item, expected) for item in actual)
        return actual == expected

    async def resolve_connector_credential_for_owner(
            self,
            *,
            owner_user_id: str,
            provider_key: str,
            filters: dict[str, Any] | None = None,
            status: str | None = "active",
    ) -> dict[str, Any]:
        canonical, capability = self.resolve_connector_capability(provider_key)
        if canonical is None or capability is None:
            return {
                "status": "error",
                "error": f"Connector '{provider_key}' not found.",
                "provider": provider_key,
                "filters": filters or {},
                "candidates": [],
            }

        normalized_status = status.strip().lower() if isinstance(status, str) and status.strip() else None
        requested_filters = filters if isinstance(filters, dict) else {}
        credentials = await self.list_credentials_for_owner(owner_user_id)
        candidates: list[CredentialDefinition] = []
        for credential in credentials:
            credential_provider = normalize_connector_provider_key(credential.provider or "")
            if credential_provider != canonical:
                continue
            credential_status = (
                credential.status.value
                if hasattr(credential.status, "value")
                else str(credential.status)
            )
            if normalized_status and credential_status.lower() != normalized_status:
                continue
            if all(
                    self._connector_filter_matches(credential.metadata.get(key), expected)
                    for key, expected in requested_filters.items()
            ):
                candidates.append(credential)

        summaries = [self.connector_credential_summary(credential) for credential in candidates]
        base = {
            "provider": canonical,
            "filters": requested_filters,
            "match_count": len(candidates),
            "candidates": summaries,
        }
        if len(candidates) == 1:
            return {"status": "matched", "credential": summaries[0], **base}
        if len(candidates) > 1:
            return {
                "status": "ambiguous",
                "error": (
                    "Multiple connector credentials match; add instance identity "
                    "filters or choose a credential_id."
                ),
                **base,
            }
        return {
            "status": "not_found",
            "error": "No connector credential matched the requested provider and filters.",
            **base,
        }

    async def get_credential_for_owner(self, credential_id: str, owner_user_id: str) -> CredentialDefinition | None:
        item = await self.context.credential_repo.get(credential_id)
        if item is None or item.owner_user_id != owner_user_id:
            return None
        return item

    async def update_credential_for_owner(
            self,
            *,
            credential_id: str,
            owner_user_id: str,
            patch: dict[str, Any],
    ) -> CredentialDefinition | None:
        existing = await self.get_credential_for_owner(credential_id, owner_user_id)
        if existing is None:
            return None
        await self._raise_for_credential_payload_errors(
            patch,
            owner_user_id,
            provider=str(patch.get("provider") or existing.provider or ""),
        )
        sanitized_patch = {key: value for key, value in patch.items() if key != "owner_user_id"}
        return await self.context.credential_repo.update(credential_id, sanitized_patch)

    async def update_connector_credential_for_owner(
            self,
            *,
            credential_id: str,
            owner_user_id: str,
            patch: dict[str, Any],
    ) -> CredentialDefinition | None:
        existing = await self.get_credential_for_owner(credential_id, owner_user_id)
        if existing is None:
            return None

        provider_key = patch.get("provider") or existing.provider
        canonical, capability = self.resolve_connector_capability(str(provider_key) if provider_key is not None else "")
        if canonical is None or capability is None:
            return None
        await self._raise_for_credential_payload_errors(patch, owner_user_id, provider=canonical)

        sanitized_patch = {key: value for key, value in patch.items() if key != "owner_user_id"}
        sanitized_patch["provider"] = canonical
        return await self.context.credential_repo.update(credential_id, sanitized_patch)

    async def revoke_credential_for_owner(self, credential_id: str, owner_user_id: str) -> CredentialDefinition | None:
        existing = await self.get_credential_for_owner(credential_id, owner_user_id)
        if existing is None:
            return None
        revoked = await self.context.credential_repo.update(
            credential_id,
            {
                "status": CredentialStatus.REVOKED,
                "revoked_at": datetime.now(timezone.utc),
            },
        )
        if revoked is not None and is_onecli_secret_ref(existing.secret_ref):
            await OneCLIIdentityMappingService(self.context).disable_active_for_owner(
                owner_user_id,
                actor_user_id=owner_user_id,
                reason="credential_revoked",
                credential_id=credential_id,
            )
        return revoked

    async def rotate_credential_for_owner(
            self,
            *,
            credential_id: str,
            owner_user_id: str,
            payload: dict[str, Any],
    ) -> CredentialDefinition | None:
        existing = await self.get_credential_for_owner(credential_id, owner_user_id)
        if existing is None:
            return None
        await self._raise_for_credential_payload_errors(payload, owner_user_id, provider=existing.provider)

        patch: dict[str, Any] = {
            "status": CredentialStatus.ACTIVE,
            "last_rotated_at": datetime.now(timezone.utc),
            "revoked_at": None,
            "secret_version": existing.secret_version + 1,
        }

        secret_ref = payload.get("secret_ref")
        if isinstance(secret_ref, str) and secret_ref.strip():
            patch["secret_ref"] = secret_ref.strip()

        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            patch["metadata"] = {**existing.metadata, **metadata}

        rotation_policy = payload.get("rotation_policy")
        if isinstance(rotation_policy, dict):
            patch["rotation_policy"] = rotation_policy

        return await self.context.credential_repo.update(credential_id, patch)

    async def delete_credential_for_owner(self, credential_id: str, owner_user_id: str) -> bool:
        existing = await self.get_credential_for_owner(credential_id, owner_user_id)
        if existing is None:
            return False
        deleted = await self.context.credential_repo.soft_delete(credential_id)
        if deleted and is_onecli_secret_ref(existing.secret_ref):
            await OneCLIIdentityMappingService(self.context).disable_active_for_owner(
                owner_user_id,
                actor_user_id=owner_user_id,
                reason="credential_deleted",
                credential_id=credential_id,
            )
        return deleted
