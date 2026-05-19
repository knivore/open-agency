from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.api.context import ApiContext
from app.core.config import get_settings
from app.domain import CredentialDefinition, CredentialStatus
from app.integrations import normalize_connector_provider_key, resolve_secret_ref


@dataclass(slots=True)
class ChannelWebhookVerificationService:
    context: ApiContext

    async def verify(
        self,
        *,
        provider: str,
        credential_id: str | None,
        headers: dict[str, str],
        body: bytes,
    ) -> dict[str, Any]:
        normalized = provider.strip().lower()
        credential = await self._credential_for(provider=normalized, credential_id=credential_id)
        if credential is None:
            if get_settings().app_env == "production":
                raise ValueError("A connector credential is required to verify production webhooks.")
            return {"verified": False, "required": False, "reason": "No connector credential supplied."}

        if credential.status != CredentialStatus.ACTIVE:
            raise ValueError(f"Credential '{credential.id}' is not active.")

        if normalized == "telegram":
            return self._verify_telegram(headers=headers, credential=credential)
        if normalized == "discord":
            return self._verify_discord(headers=headers, body=body, credential=credential)
        if normalized == "whatsapp":
            return self._verify_whatsapp(headers=headers, body=body, credential=credential)
        raise ValueError(f"Unsupported chat channel adapter '{provider}'")

    async def _credential_for(self, *, provider: str, credential_id: str | None) -> CredentialDefinition | None:
        if not credential_id:
            return None
        credential = await self.context.credential_repo.get(credential_id)
        if credential is None:
            raise ValueError("Connector credential not found.")
        expected = self._expected_credential_provider(provider)
        if normalize_connector_provider_key(credential.provider) != expected:
            raise ValueError(f"Credential '{credential_id}' is not configured for {provider}.")
        return credential

    def _expected_credential_provider(self, provider: str) -> str | None:
        if provider == "telegram":
            return "telegram-bot"
        if provider == "discord":
            return "discord-bot"
        if provider == "whatsapp":
            return "whatsapp-cloud-api"
        raise ValueError(f"Unsupported chat channel adapter '{provider}'")

    def _verify_telegram(self, *, headers: dict[str, str], credential: CredentialDefinition) -> dict[str, Any]:
        expected = self._metadata_secret(credential, "webhook_secret_ref", "webhook_secret_token")
        if expected is None:
            return self._not_required_or_production_error("Telegram credential metadata requires webhook_secret_ref.")
        actual = headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(actual, expected):
            raise ValueError("Telegram webhook secret token verification failed.")
        return {"verified": True, "required": True, "provider": "telegram", "credential_id": credential.id}

    def _verify_discord(self, *, headers: dict[str, str], body: bytes, credential: CredentialDefinition) -> dict[str, Any]:
        public_key = str(credential.metadata.get("webhook_public_key") or "").strip()
        if not public_key:
            return self._not_required_or_production_error("Discord credential metadata requires webhook_public_key.")
        signature = headers.get("x-signature-ed25519", "")
        timestamp = headers.get("x-signature-timestamp", "")
        if not signature or not timestamp:
            raise ValueError("Discord signature headers are required.")
        try:
            verify_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
            verify_key.verify(bytes.fromhex(signature), timestamp.encode("utf-8") + body)
        except (ValueError, InvalidSignature) as exc:
            raise ValueError("Discord webhook signature verification failed.") from exc
        return {"verified": True, "required": True, "provider": "discord", "credential_id": credential.id}

    def _verify_whatsapp(self, *, headers: dict[str, str], body: bytes, credential: CredentialDefinition) -> dict[str, Any]:
        app_secret = self._metadata_secret(credential, "app_secret_ref", "app_secret")
        if app_secret is None:
            return self._not_required_or_production_error("WhatsApp credential metadata requires app_secret_ref.")
        signature = headers.get("x-hub-signature-256", "")
        if not signature.startswith("sha256="):
            raise ValueError("WhatsApp x-hub-signature-256 header is required.")
        expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("WhatsApp webhook signature verification failed.")
        return {"verified": True, "required": True, "provider": "whatsapp", "credential_id": credential.id}

    def _metadata_secret(
        self,
        credential: CredentialDefinition,
        ref_key: str,
        fallback_key: str,
    ) -> str | None:
        secret_ref = credential.metadata.get(ref_key)
        if isinstance(secret_ref, str) and secret_ref.strip():
            resolved = resolve_secret_ref(secret_ref.strip())
            if resolved.value is None:
                raise ValueError(resolved.error or f"Could not resolve {ref_key}.")
            return resolved.value

        value = credential.metadata.get(fallback_key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _not_required_or_production_error(self, reason: str) -> dict[str, Any]:
        if get_settings().app_env == "production":
            raise ValueError(reason)
        return {"verified": False, "required": False, "reason": reason}


__all__ = ["ChannelWebhookVerificationService"]
