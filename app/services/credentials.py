from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pydantic import ValidationError
from typing import Any

from app.api.context import ApiContext
from app.domain import ConnectorCredentialValidationPayload, CredentialDefinition, CredentialStatus
from app.integrations import get_connector_definition, normalize_connector_provider_key
from app.services.integrations_registry import IntegrationsRegistryService

RAW_SECRET_PAYLOAD_KEYS = {"secret", "raw_secret", "value", "token", "password", "api_key"}


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
        raw_keys = sorted(RAW_SECRET_PAYLOAD_KEYS.intersection({key.lower() for key in payload}))
        if not raw_keys:
            return []
        return ["Raw secret material must be stored in the secret store and referenced by secret_ref"]

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
        errors = self.raw_secret_payload_errors(payload)
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
        if self.raw_secret_payload_errors(payload):
            raise ValueError("Raw secret material must be stored in the secret store and referenced by secret_ref")
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
        if self.raw_secret_payload_errors(patch):
            raise ValueError("Raw secret material must be stored in the secret store and referenced by secret_ref")

        sanitized_patch = {key: value for key, value in patch.items() if key != "owner_user_id"}
        sanitized_patch["provider"] = canonical
        return await self.context.credential_repo.update(credential_id, sanitized_patch)

    async def revoke_credential_for_owner(self, credential_id: str, owner_user_id: str) -> CredentialDefinition | None:
        existing = await self.get_credential_for_owner(credential_id, owner_user_id)
        if existing is None:
            return None
        return await self.context.credential_repo.update(
            credential_id,
            {
                "status": CredentialStatus.REVOKED,
                "revoked_at": datetime.now(timezone.utc),
            },
        )

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
        if self.raw_secret_payload_errors(payload):
            raise ValueError("Raw secret material must be stored in the secret store and referenced by secret_ref")

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
        return await self.context.credential_repo.soft_delete(credential_id)
