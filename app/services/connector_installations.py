from __future__ import annotations

import httpx
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from app.api.context import ApiContext
from app.core.config import get_settings
from app.domain import ConnectorInstallation, ConnectorSetupSessionPayload, CredentialDefinition
from app.integrations.connectors import get_connector_definition, normalize_connector_provider_key
from app.integrations.secrets import resolve_secret_ref
from app.services.connectors import ConnectorService
from app.services.credentials import RAW_SECRET_PAYLOAD_KEYS
from app.services.integrations_registry import IntegrationsRegistryService
from app.services.public_endpoints import PublicEndpointService
from app.services.runtime_secrets import open_runtime_secret, seal_runtime_secret

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConnectorInstallationService:
    context: ApiContext

    def _runtime_secret_required_for_provider(self, provider: str) -> bool:
        # Telegram must stay direct because the token is embedded in the URL path.
        # Discord can keep working through owner-scoped OneCLI proxy delivery and
        # REST polling, while mirrored runtime secrets remain optional for more
        # direct Gateway-style behavior.
        return provider == "telegram-bot"

    def _raw_secret_payload_errors(self, payload: dict[str, Any]) -> list[str]:
        raw_keys = sorted(RAW_SECRET_PAYLOAD_KEYS.intersection({key.lower() for key in payload}))
        if not raw_keys:
            return []
        return ["Raw upstream secrets must be entered through OneCLI setup, not Agency."]

    def _raise_for_raw_secret_payload(self, payload: dict[str, Any]) -> None:
        errors = self._raw_secret_payload_errors(payload)
        if errors:
            raise ValueError(errors[0])

    def _resolve_provider(self, provider_key: str) -> str:
        canonical = normalize_connector_provider_key(provider_key)
        if canonical is None or get_connector_definition(canonical) is None:
            raise LookupError(f"Connector '{provider_key}' not found")
        capabilities = IntegrationsRegistryService().list_connector_capabilities()
        if canonical not in capabilities.connectors:
            raise LookupError(f"Connector '{provider_key}' not found")
        return canonical

    def _default_onecli_ref(self, *, owner_user_id: str, provider: str, installation_id: str) -> str:
        return f"onecli://users/{owner_user_id}/{provider}/{installation_id}"

    def _runtime_secret_ref(self, installation_id: str) -> str:
        return f"secret://agency/installations/{installation_id}"

    def _transport_mode(self, provider: str) -> str:
        definition = get_connector_definition(provider)
        if definition is None:
            return "proxy"
        return definition.onecli_transport_mode

    def _device_code(self, installation_id: str) -> str:
        return installation_id.replace("-", "")[:10].upper()

    def _setup_url(
            self,
            *,
            provider: str,
            installation_id: str,
            device_code: str,
            owner_user_id: str,
            onecli_credential_ref: str,
    ) -> str:
        base_url = get_settings().onecli_api_url.rstrip("/")
        query = urlencode(
            {
                "agency_installation_id": installation_id,
                "agency_user_id": owner_user_id,
                "device_code": device_code,
                "onecli_credential_ref": onecli_credential_ref,
                "provider": provider,
            }
        )
        return f"{base_url}/?{query}"

    def _ensure_owner_scoped_onecli_ref(self, *, owner_user_id: str, onecli_credential_ref: str) -> None:
        owner_prefix = f"onecli://users/{owner_user_id}/"
        if not onecli_credential_ref.startswith(owner_prefix):
            raise ValueError(f"OneCLI credential refs must be scoped under {owner_prefix}.")

    async def _public_webhook_base_url(self) -> str | None:
        base_url = str(get_settings().agency_public_webhook_base_url or "").strip()
        if not base_url:
            base_url = await PublicEndpointService(self.context).get_current_webhook_base_url() or ""
        if not base_url:
            return None
        return base_url.rstrip("/")

    async def _telegram_webhook_url(self, installation_id: str) -> str | None:
        base_url = await self._public_webhook_base_url()
        if base_url is None:
            return None
        return (
            f"{base_url}/integrations/conversations/adapters/telegram/webhook"
            f"?credential_id={installation_id}"
        )

    def _telegram_webhook_secret_token(self, metadata: dict[str, Any]) -> str | None:
        token = metadata.get("webhook_secret_token")
        if isinstance(token, str) and token.strip():
            return token.strip()

        secret_ref = metadata.get("webhook_secret_ref")
        if isinstance(secret_ref, str) and secret_ref.strip():
            resolved = resolve_secret_ref(secret_ref.strip())
            if resolved.value is None:
                raise ValueError(resolved.error or "Could not resolve webhook_secret_ref.")
            return resolved.value
        return None

    def _telegram_webhook_request_payload(self, installation: ConnectorInstallation) -> dict[str, Any]:
        # setWebhook should point Telegram back to the launcher-discovered public
        # Agency base URL so direct bot completion works after startup.
        return {}

    async def _register_telegram_webhook(self, installation: ConnectorInstallation) -> dict[str, Any] | None:
        if installation.status != "active":
            raise ValueError("Telegram webhook auto-registration requires an active installation.")
        webhook_url = await self._telegram_webhook_url(installation.id)
        if webhook_url is None:
            return None
        if not installation.runtime_secret_encrypted:
            raise ValueError("Telegram webhook auto-registration requires a mirrored runtime secret.")

        token = open_runtime_secret(installation.runtime_secret_encrypted)
        if not token:
            raise ValueError("Telegram webhook auto-registration requires a readable runtime secret.")

        request_payload = self._telegram_webhook_request_payload(installation)
        request_payload["url"] = webhook_url
        webhook_secret_token = self._telegram_webhook_secret_token(installation.metadata)
        if webhook_secret_token is not None:
            request_payload["secret_token"] = webhook_secret_token
        webhook_registration_url = f"https://api.telegram.org/bot{token}/setWebhook"
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            response = await client.post(webhook_registration_url, data=request_payload)
        try:
            body = response.json()
        except ValueError:
            body = {"description": response.text}
        if response.status_code != 200 or not isinstance(body, dict) or not body.get("ok"):
            description = ""
            if isinstance(body.get("description"), str):
                description = body["description"]
            elif isinstance(body.get("error"), dict) and isinstance(body["error"].get("message"), str):
                description = body["error"]["message"]
            raise ValueError(description or f"Telegram setWebhook returned HTTP {response.status_code}.")
        return body

    def _setup_session_payload(self, installation: ConnectorInstallation) -> ConnectorSetupSessionPayload:
        session_id = installation.setup_session_id or installation.id
        device_code = self._device_code(session_id)
        return ConnectorSetupSessionPayload(
            installation=installation,
            setup_url=self._setup_url(
                provider=installation.provider,
                installation_id=installation.id,
                device_code=device_code,
                owner_user_id=installation.owner_user_id,
                onecli_credential_ref=installation.onecli_credential_ref,
            ),
            device_code=device_code,
            onecli_credential_ref=installation.onecli_credential_ref,
        )

    async def _list_all_for_owner(self, owner_user_id: str) -> list[ConnectorInstallation]:
        if hasattr(self.context.connector_installation_repo, "list_by_owner"):
            return await self.context.connector_installation_repo.list_by_owner(owner_user_id)
        return [
            item
            for item in await self.context.connector_installation_repo.list()
            if item.owner_user_id == owner_user_id
        ]

    async def list_for_owner(self, owner_user_id: str) -> list[ConnectorInstallation]:
        return await self._list_all_for_owner(owner_user_id)

    async def get_for_owner(self, installation_id: str, owner_user_id: str) -> ConnectorInstallation | None:
        item = await self.context.connector_installation_repo.get(installation_id)
        if item is None or item.owner_user_id != owner_user_id:
            return None
        return item

    async def create_setup_session(
            self,
            *,
            provider_key: str,
            payload: dict[str, Any],
            owner_user_id: str,
    ) -> ConnectorSetupSessionPayload:
        self._raise_for_raw_secret_payload(payload)
        provider = self._resolve_provider(provider_key)
        name = str(payload.get("name") or provider).strip() or provider
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        workflow_id = payload.get("workflow_id") if isinstance(payload.get("workflow_id"), str) else None
        installation_id = str(uuid4())
        onecli_credential_ref = self._default_onecli_ref(
            owner_user_id=owner_user_id,
            provider=provider,
            installation_id=installation_id,
        )

        installation = ConnectorInstallation(
            id=installation_id,
            owner_user_id=owner_user_id,
            workflow_id=workflow_id,
            provider=provider,
            name=name,
            onecli_credential_ref=onecli_credential_ref,
            setup_session_id=installation_id,
            status="setup_pending",
            metadata=metadata,
        )
        saved = await self.context.connector_installation_repo.create(installation)
        return self._setup_session_payload(saved)

    async def complete_for_owner(
            self,
            *,
            installation_id: str,
            owner_user_id: str,
            payload: dict[str, Any],
    ) -> ConnectorInstallation | None:
        self._raise_for_raw_secret_payload(payload)
        current = await self.get_for_owner(installation_id, owner_user_id)
        if current is None:
            return None
        transport_mode = self._transport_mode(current.provider)
        onecli_credential_ref = str(
            payload.get("onecli_credential_ref") or current.onecli_credential_ref
        ).strip()
        self._ensure_owner_scoped_onecli_ref(
            owner_user_id=owner_user_id,
            onecli_credential_ref=onecli_credential_ref,
        )
        runtime_secret_value = payload.get("runtime_secret_value")
        runtime_secret_encrypted = current.runtime_secret_encrypted
        if isinstance(runtime_secret_value, str) and runtime_secret_value.strip():
            if transport_mode != "direct":
                raise ValueError("runtime_secret_value is only used for direct transport connectors.")
            runtime_secret_encrypted = seal_runtime_secret(runtime_secret_value)
        elif (
                transport_mode == "direct"
                and self._runtime_secret_required_for_provider(current.provider)
                and not runtime_secret_encrypted
        ):
            raise ValueError(
                "Direct transport requires runtime_secret_value so Agency can resolve the secret at request time."
            )
        metadata = current.metadata
        if isinstance(payload.get("metadata"), dict):
            metadata = {**metadata, **payload["metadata"]}
        if current.provider == "telegram-bot":
            webhook_secret_token = self._telegram_webhook_secret_token(metadata)
            if webhook_secret_token is None:
                metadata = {**metadata, "webhook_secret_token": secrets.token_urlsafe(32)}

        patch: dict[str, Any] = {
            "onecli_credential_ref": onecli_credential_ref,
            "runtime_secret_encrypted": runtime_secret_encrypted,
            "metadata": metadata,
            "status": "active",
        }
        if current.status == "rotation_required":
            patch["last_rotated_at"] = datetime.now(timezone.utc)

        updated = await self.context.connector_installation_repo.update(
            installation_id,
            patch,
        )
        if updated is None:
            return None

        await self._save_legacy_credential_projection(updated)
        if updated.provider == "telegram-bot":
            # Telegram webhook registration lives at completion time so the
            # launcher can supply the public URL once and keep provider calls
            # direct without a separate manual setWebhook step.
            await self._register_telegram_webhook(updated)
        return updated

    async def _save_legacy_credential_projection(self, installation: ConnectorInstallation) -> None:
        secret_ref = (
            self._runtime_secret_ref(installation.id)
            if installation.runtime_secret_encrypted
            else installation.onecli_credential_ref
        )
        credential = CredentialDefinition(
            id=installation.id,
            owner_user_id=installation.owner_user_id,
            name=installation.name,
            provider=installation.provider,
            secret_ref=secret_ref,
            status="active",
            metadata=installation.metadata,
        )
        if await self.context.credential_repo.get(credential.id, include_deleted=True):
            await self.context.credential_repo.save(credential)
        else:
            await self.context.credential_repo.create(credential)

    async def test_for_owner(self, installation_id: str, owner_user_id: str) -> dict[str, Any] | None:
        installation = await self.get_for_owner(installation_id, owner_user_id)
        if installation is None:
            return None
        if installation.status != "active":
            return {
                "ok": False,
                "target_type": "connector_installation",
                "target_id": installation.id,
                "provider": installation.provider,
                "error": "Connector installation is not active.",
            }
        await self._save_legacy_credential_projection(installation)
        return await ConnectorService(self.context).test_credential_for_owner(
            installation.id,
            owner_user_id,
        )

    async def rotate_for_owner(
            self,
            *,
            installation_id: str,
            owner_user_id: str,
            payload: dict[str, Any],
    ) -> ConnectorSetupSessionPayload | None:
        self._raise_for_raw_secret_payload(payload)
        current = await self.get_for_owner(installation_id, owner_user_id)
        if current is None:
            return None
        if current.status in {"revoked", "disabled"}:
            raise ValueError("Revoked or disabled connector installations cannot be rotated.")
        transport_mode = self._transport_mode(current.provider)
        onecli_credential_ref = str(
            payload.get("onecli_credential_ref") or current.onecli_credential_ref
        ).strip()
        self._ensure_owner_scoped_onecli_ref(
            owner_user_id=owner_user_id,
            onecli_credential_ref=onecli_credential_ref,
        )
        runtime_secret_value = payload.get("runtime_secret_value")
        runtime_secret_encrypted = current.runtime_secret_encrypted
        if isinstance(runtime_secret_value, str) and runtime_secret_value.strip():
            if transport_mode != "direct":
                raise ValueError("runtime_secret_value is only used for direct transport connectors.")
            runtime_secret_encrypted = seal_runtime_secret(runtime_secret_value)
        elif (
                transport_mode == "direct"
                and self._runtime_secret_required_for_provider(current.provider)
                and not runtime_secret_encrypted
        ):
            raise ValueError(
                "Direct transport requires runtime_secret_value so Agency can resolve the secret at request time."
            )

        metadata = current.metadata
        if isinstance(payload.get("metadata"), dict):
            metadata = {**metadata, **payload["metadata"]}
        if current.provider == "telegram-bot":
            webhook_secret_token = self._telegram_webhook_secret_token(metadata)
            if webhook_secret_token is None:
                metadata = {**metadata, "webhook_secret_token": secrets.token_urlsafe(32)}

        session_id = str(uuid4())
        updated = await self.context.connector_installation_repo.update(
            installation_id,
            {
                "onecli_credential_ref": onecli_credential_ref,
                "runtime_secret_encrypted": runtime_secret_encrypted,
                "metadata": metadata,
                "status": "rotation_required",
                "setup_session_id": session_id,
            },
        )
        if updated is None:
            return None
        await self._save_legacy_credential_projection(updated)
        return self._setup_session_payload(updated)

    async def revoke_for_owner(self, installation_id: str, owner_user_id: str) -> ConnectorInstallation | None:
        current = await self.get_for_owner(installation_id, owner_user_id)
        if current is None:
            return None
        return await self.context.connector_installation_repo.update(
            installation_id,
            {
                "status": "revoked",
                "revoked_at": datetime.now(timezone.utc),
            },
        )

    async def reconcile_startup_integrations(self) -> dict[str, int]:
        public_base_url = await self._public_webhook_base_url()
        if not public_base_url:
            return {"telegram_webhooks_reconciled": 0, "telegram_webhook_errors": 0}

        reconciled = 0
        errors = 0
        installations = await self.context.connector_installation_repo.list()
        for installation in installations:
            if installation.status != "active":
                continue
            if installation.provider != "telegram-bot":
                continue
            try:
                result = await self._register_telegram_webhook(installation)
                if result is not None:
                    reconciled += 1
            except Exception as exc:
                errors += 1
                logger.warning(
                    "Telegram webhook reconciliation failed for installation '%s': %s",
                    installation.id,
                    exc,
                )
        return {
            "telegram_webhooks_reconciled": reconciled,
            "telegram_webhook_errors": errors,
        }
