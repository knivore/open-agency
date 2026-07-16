from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx

from app.api.context import ApiContext
from app.core.config import get_settings
from app.domain import ConnectorInstallation, ConnectorSetupSessionPayload, CredentialDefinition
from app.integrations.connectors import (
    get_connector_definition,
    normalize_connector_provider_key,
    validate_connector_metadata,
)
from app.integrations.onecli_catalog import (
    ONECLI_NATIVE_APP_BY_CONNECTOR,
    ONECLI_SECRET_PROFILE_BY_CONNECTOR,
    onecli_resource_name,
)
from app.integrations.secrets import resolve_secret_ref
from app.services.connectors import ConnectorService
from app.services.credentials import CredentialService, RAW_SECRET_PAYLOAD_KEYS
from app.services.integrations_registry import IntegrationsRegistryService
from app.services.onecli_control import OneCLIControlClient
from app.services.public_endpoints import PublicEndpointService

logger = logging.getLogger(__name__)

_RAW_METADATA_SECRET_KEYS = {
    "access_token",
    "api_token",
    "app_secret",
    "bot_token",
    "client_secret",
    "private_key",
    "refresh_token",
    "signing_secret",
    "webhook_secret",
    "webhook_secret_token",
}


@dataclass(slots=True)
class ConnectorInstallationService:
    context: ApiContext

    def _raw_secret_payload_errors(self, payload: dict[str, Any]) -> list[str]:
        raw_keys = sorted(RAW_SECRET_PAYLOAD_KEYS.intersection({key.lower() for key in payload}))
        metadata = payload.get("metadata")
        raw_metadata_keys = (
            sorted(
                (RAW_SECRET_PAYLOAD_KEYS | _RAW_METADATA_SECRET_KEYS).intersection(
                    {str(key).lower() for key in metadata}
                )
            )
            if isinstance(metadata, dict)
            else []
        )
        if raw_keys or raw_metadata_keys:
            return [
                "Raw upstream secrets must be entered through OneCLI setup; Agency metadata accepts only secret references."
            ]
        return []

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
        # OneCLI does not consume Agency identity/session query parameters. Keep
        # them out of browser history and let the frontend add only documented
        # OneCLI setup-prefill parameters.
        return f"{get_settings().onecli_api_url.rstrip('/')}/"

    def _setup_window(self) -> tuple[datetime, datetime]:
        started_at = datetime.now(timezone.utc)
        expires_at = started_at + timedelta(seconds=get_settings().onecli_setup_session_ttl_seconds)
        return started_at, expires_at

    def _require_live_setup_session(self, installation: ConnectorInstallation) -> datetime:
        if installation.status not in {"setup_pending", "rotation_required"}:
            raise ValueError("This connector does not have a setup session awaiting verification.")
        if installation.setup_started_at is None or installation.setup_expires_at is None:
            raise ValueError("This setup session is missing its verification window. Start setup again.")
        now = datetime.now(timezone.utc)
        expires_at = installation.setup_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if now >= expires_at:
            raise ValueError("This setup session has expired. Start setup again to create a fresh session.")
        started_at = installation.setup_started_at
        return started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)

    async def _verified_onecli_ref(self, installation: ConnectorInstallation) -> str:
        started_at = self._require_live_setup_session(installation)
        client = OneCLIControlClient.from_settings()
        native_app_id = ONECLI_NATIVE_APP_BY_CONNECTOR.get(installation.provider)
        if native_app_id:
            verified = await client.verify_connection(provider=native_app_id, started_at=started_at)
        else:
            profile = ONECLI_SECRET_PROFILE_BY_CONNECTOR.get(installation.provider)
            if profile is None:
                raise ValueError(
                    f"Connector '{installation.provider}' does not have a verified OneCLI setup contract."
                )
            verified = await client.verify_secret(
                resource_name=onecli_resource_name(
                    installation.provider,
                    installation.setup_session_id or installation.id,
                ),
                started_at=started_at,
                profile=profile,
            )
        return f"onecli://users/{installation.owner_user_id}/{verified.kind}/{verified.id}"

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

    @staticmethod
    def _telegram_webhook_secret_hash(token: str) -> str:
        # Inbound verification only needs a one-way verifier. Keeping the
        # generated token out of installation metadata prevents API/UI exposure.
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _telegram_metadata_without_plaintext_token(
            self,
            metadata: dict[str, Any],
            *,
            token: str,
    ) -> dict[str, Any]:
        scrubbed = {key: value for key, value in metadata.items() if key != "webhook_secret_token"}
        secret_ref = scrubbed.get("webhook_secret_ref")
        if isinstance(secret_ref, str) and secret_ref.strip():
            scrubbed.pop("webhook_secret_token_sha256", None)
        else:
            scrubbed["webhook_secret_token_sha256"] = self._telegram_webhook_secret_hash(token)
        return scrubbed

    def _telegram_webhook_request_payload(self, installation: ConnectorInstallation) -> dict[str, Any]:
        # setWebhook should point Telegram back to the launcher-discovered public
        # Agency base URL so direct bot completion works after startup.
        return {}

    async def _register_telegram_webhook(
            self,
            installation: ConnectorInstallation,
            *,
            secret_token: str | None = None,
    ) -> dict[str, Any] | None:
        if installation.status != "active":
            raise ValueError("Telegram webhook auto-registration requires an active installation.")
        webhook_url = await self._telegram_webhook_url(installation.id)
        if webhook_url is None:
            return None

        request_payload = self._telegram_webhook_request_payload(installation)
        request_payload["url"] = webhook_url
        webhook_secret_token = secret_token or self._telegram_webhook_secret_token(installation.metadata)
        if webhook_secret_token is not None:
            request_payload["secret_token"] = webhook_secret_token
        # OneCLI v1.40+ can replace a placeholder in the URL path. This keeps
        # the Telegram bot token in OneCLI while preserving automatic webhook
        # registration during setup and launcher reconciliation.
        webhook_registration_url = "https://api.telegram.org/botonecli-managed/setWebhook"
        proxy_kwargs = await ConnectorService(self.context).onecli_proxy_kwargs_for_owner(
            installation.owner_user_id
        )
        async with httpx.AsyncClient(timeout=10.0, **proxy_kwargs) as client:
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
            onecli_resource_name=onecli_resource_name(installation.provider, session_id),
            expires_at=installation.setup_expires_at,
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
        if "onecli_credential_ref" in payload or "runtime_secret_value" in payload:
            raise ValueError(
                "Agency creates and verifies the OneCLI setup resource; do not submit a credential ref or secret."
            )
        provider = self._resolve_provider(provider_key)
        if (
                provider not in ONECLI_NATIVE_APP_BY_CONNECTOR
                and provider not in ONECLI_SECRET_PROFILE_BY_CONNECTOR
        ):
            raise ValueError(
                "This connector's guide requires a OneCLI setup shape that cannot yet be verified. "
                "Use the guide for preparation, but do not create an Agency installation yet."
            )
        name = str(payload.get("name") or provider).strip() or provider
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        workflow_id = payload.get("workflow_id") if isinstance(payload.get("workflow_id"), str) else None
        now = datetime.now(timezone.utc)
        for existing in await self._list_all_for_owner(owner_user_id):
            if existing.provider != provider or existing.status not in {"setup_pending", "rotation_required"}:
                continue
            expires_at = existing.setup_expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at is None or expires_at <= now:
                # Expired pre-verification records cannot become active and
                # should not accumulate as ambiguous resumable installations.
                await self.context.connector_installation_repo.update(
                    existing.id,
                    {"status": "revoked", "revoked_at": now},
                )
        installation_id = str(uuid4())
        onecli_credential_ref = self._default_onecli_ref(
            owner_user_id=owner_user_id,
            provider=provider,
            installation_id=installation_id,
        )
        setup_started_at, setup_expires_at = self._setup_window()

        installation = ConnectorInstallation(
            id=installation_id,
            owner_user_id=owner_user_id,
            workflow_id=workflow_id,
            provider=provider,
            name=name,
            onecli_credential_ref=onecli_credential_ref,
            setup_session_id=installation_id,
            setup_started_at=setup_started_at,
            setup_expires_at=setup_expires_at,
            status="setup_pending",
            metadata=metadata,
        )
        saved = await self.context.connector_installation_repo.create(installation)
        return self._setup_session_payload(saved)

    async def resume_setup_session_for_owner(
            self,
            installation_id: str,
            owner_user_id: str,
    ) -> ConnectorSetupSessionPayload | None:
        installation = await self.get_for_owner(installation_id, owner_user_id)
        if installation is None:
            return None
        self._require_live_setup_session(installation)
        return self._setup_session_payload(installation)

    async def complete_for_owner(
            self,
            *,
            installation_id: str,
            owner_user_id: str,
            payload: dict[str, Any],
    ) -> ConnectorInstallation | None:
        self._raise_for_raw_secret_payload(payload)
        if "onecli_credential_ref" in payload or "runtime_secret_value" in payload:
            raise ValueError(
                "Agency determines the OneCLI resource during verification; do not submit a credential ref or secret."
            )
        current = await self.get_for_owner(installation_id, owner_user_id)
        if current is None:
            return None
        metadata = current.metadata
        if isinstance(payload.get("metadata"), dict):
            metadata = {**metadata, **payload["metadata"]}
        telegram_webhook_secret_token: str | None = None
        if current.provider == "telegram-bot":
            telegram_webhook_secret_token = (
                self._telegram_webhook_secret_token(metadata) or secrets.token_urlsafe(32)
            )
            # Connector metadata validation still sees the transient token, but
            # the durable record below retains only its one-way verifier.
            metadata = {**metadata, "webhook_secret_token": telegram_webhook_secret_token}
        metadata_errors = validate_connector_metadata(current.provider, metadata)
        if metadata_errors:
            raise ValueError(metadata_errors[0])

        if telegram_webhook_secret_token is not None:
            metadata = self._telegram_metadata_without_plaintext_token(
                metadata,
                token=telegram_webhook_secret_token,
            )

        # The browser never chooses this reference. It is bound to the resource
        # id returned by OneCLI's metadata-only control API after exact matching.
        onecli_credential_ref = await self._verified_onecli_ref(current)

        patch: dict[str, Any] = {
            "onecli_credential_ref": onecli_credential_ref,
            "runtime_secret_encrypted": None,
            "metadata": metadata,
            "status": "active",
        }
        if current.status == "rotation_required":
            patch["last_rotated_at"] = datetime.now(timezone.utc)

        if current.provider == "telegram-bot":
            # Keep the durable record pending when provider-side registration
            # fails, so the user can correct OneCLI and retry verification.
            candidate = current.model_copy(update=patch)
            await self._register_telegram_webhook(
                candidate,
                secret_token=telegram_webhook_secret_token,
            )

        updated = await self.context.connector_installation_repo.update(
            installation_id,
            patch,
        )
        if updated is None:
            return None

        await self._save_legacy_credential_projection(updated)
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
        if "onecli_credential_ref" in payload or "runtime_secret_value" in payload:
            raise ValueError(
                "Agency creates a fresh OneCLI verification session; do not submit a credential ref or secret."
            )
        current = await self.get_for_owner(installation_id, owner_user_id)
        if current is None:
            return None
        if current.status in {"revoked", "disabled"}:
            raise ValueError("Revoked or disabled connector installations cannot be rotated.")
        metadata = current.metadata
        if isinstance(payload.get("metadata"), dict):
            metadata = {**metadata, **payload["metadata"]}
        if current.provider == "telegram-bot":
            legacy_token = metadata.get("webhook_secret_token")
            if isinstance(legacy_token, str) and legacy_token.strip():
                metadata = self._telegram_metadata_without_plaintext_token(
                    metadata,
                    token=legacy_token.strip(),
                )

        session_id = str(uuid4())
        setup_started_at, setup_expires_at = self._setup_window()
        updated = await self.context.connector_installation_repo.update(
            installation_id,
            {
                # Keep the last verified reference available until the fresh
                # rotation resource passes OneCLI verification.
                "runtime_secret_encrypted": None,
                "metadata": metadata,
                "status": "rotation_required",
                "setup_session_id": session_id,
                "setup_started_at": setup_started_at,
                "setup_expires_at": setup_expires_at,
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
        revoked = await self.context.connector_installation_repo.update(
            installation_id,
            {
                "status": "revoked",
                "revoked_at": datetime.now(timezone.utc),
            },
        )
        if revoked is not None:
            # Connector installations project into the legacy credential store
            # for agent/runtime compatibility. Revoking only one side would
            # leave the OneCLI reference usable through the other boundary.
            await CredentialService(self.context).revoke_credential_for_owner(
                installation_id,
                owner_user_id,
            )
        return revoked

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
                webhook_secret_token = (
                    self._telegram_webhook_secret_token(installation.metadata)
                    or secrets.token_urlsafe(32)
                )
                result = await self._register_telegram_webhook(
                    installation,
                    secret_token=webhook_secret_token,
                )
                if result is not None:
                    metadata = self._telegram_metadata_without_plaintext_token(
                        installation.metadata,
                        token=webhook_secret_token,
                    )
                    updated = await self.context.connector_installation_repo.update(
                        installation.id,
                        {"metadata": metadata},
                    )
                    if updated is not None:
                        await self._save_legacy_credential_projection(updated)
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
