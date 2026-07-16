"""Webhook verification helpers for inbound multichannel chat providers."""

from __future__ import annotations

import hashlib
import hmac
import time
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from dataclasses import dataclass
from typing import Any, NoReturn

from app.api.context import ApiContext
from app.domain import CredentialDefinition, CredentialStatus
from app.integrations.connectors import normalize_connector_provider_key
from app.integrations.secrets import resolve_secret_ref
from .channel_registry import chat_channel_connector_provider_key, normalize_chat_channel_provider


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
            payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = normalize_chat_channel_provider(provider) or provider.strip().lower()
        credential = await self._credential_for(provider=normalized, credential_id=credential_id)
        if credential is None:
            # Environment labels do not establish a network boundary: development
            # and test servers can still be tunneled or accidentally exposed.
            raise ValueError("A connector credential is required to verify webhooks.")

        if credential.status != CredentialStatus.ACTIVE:
            raise ValueError(f"Credential '{credential.id}' is not active.")

        if normalized == "telegram":
            return self._verify_telegram(headers=headers, credential=credential)
        if normalized == "discord":
            return self._verify_discord(headers=headers, body=body, credential=credential)
        if normalized == "whatsapp":
            return self._verify_whatsapp(headers=headers, body=body, credential=credential)
        if normalized == "slack":
            return self._verify_slack(headers=headers, body=body, credential=credential, payload=payload or {})
        if normalized == "microsoft-teams":
            return self._verify_teams(headers=headers, body=body, credential=credential)
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
        return chat_channel_connector_provider_key(provider)

    def _verify_telegram(self, *, headers: dict[str, str], credential: CredentialDefinition) -> dict[str, Any]:
        actual = headers.get("x-telegram-bot-api-secret-token", "")
        expected_hash = credential.metadata.get("webhook_secret_token_sha256")
        if isinstance(expected_hash, str) and expected_hash.strip():
            actual_hash = hashlib.sha256(actual.encode("utf-8")).hexdigest()
            verified = hmac.compare_digest(actual_hash, expected_hash.strip().lower())
        else:
            expected = self._metadata_secret(credential, "webhook_secret_ref", "webhook_secret_token")
            if expected is None:
                self._verification_configuration_error(
                    "Telegram credential metadata requires webhook_secret_ref."
                )
            verified = hmac.compare_digest(actual, expected)
        if not verified:
            raise ValueError("Telegram webhook secret token verification failed.")
        return {"verified": True, "required": True, "provider": "telegram", "credential_id": credential.id}

    def _verify_discord(self, *, headers: dict[str, str], body: bytes, credential: CredentialDefinition) -> dict[
        str, Any]:
        public_key = str(credential.metadata.get("webhook_public_key") or "").strip()
        if not public_key:
            self._verification_configuration_error("Discord credential metadata requires webhook_public_key.")
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

    def _verify_whatsapp(self, *, headers: dict[str, str], body: bytes, credential: CredentialDefinition) -> dict[
        str, Any]:
        app_secret = self._metadata_secret(credential, "app_secret_ref", "app_secret")
        if app_secret is None:
            self._verification_configuration_error("WhatsApp credential metadata requires app_secret_ref.")
        signature = headers.get("x-hub-signature-256", "")
        if not signature.startswith("sha256="):
            raise ValueError("WhatsApp x-hub-signature-256 header is required.")
        expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("WhatsApp webhook signature verification failed.")
        return {"verified": True, "required": True, "provider": "whatsapp", "credential_id": credential.id}

    def _verify_slack(
            self,
            *,
            headers: dict[str, str],
            body: bytes,
            credential: CredentialDefinition,
            payload: dict[str, Any],
    ) -> dict[str, Any]:
        signing_secret = self._metadata_secret(credential, "signing_secret_ref", "signing_secret")
        if signing_secret is None:
            self._verification_configuration_error("Slack credential metadata requires signing_secret_ref.")
        signature = headers.get("x-slack-signature", "")
        timestamp = headers.get("x-slack-request-timestamp", "")
        if not signature or not timestamp:
            raise ValueError("Slack signature headers are required.")
        try:
            timestamp_int = int(timestamp)
        except ValueError as exc:
            raise ValueError("Slack request timestamp must be an integer.") from exc
        if abs(int(time.time()) - timestamp_int) > 60 * 5:
            raise ValueError("Slack request timestamp is too old.")
        expected = "v0=" + hmac.new(
            signing_secret.encode("utf-8"),
            f"v0:{timestamp}:".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Slack webhook signature verification failed.")
        expected_team_id = str(
            credential.metadata.get("workspace_id")
            or credential.metadata.get("team_id")
            or ""
        ).strip()
        team = payload.get("team") if isinstance(payload.get("team"), dict) else {}
        actual_team_id = str(payload.get("team_id") or team.get("id") or "").strip()
        if not expected_team_id or not actual_team_id:
            raise ValueError("Slack webhook team binding is required.")
        if not hmac.compare_digest(actual_team_id, expected_team_id):
            # An app-wide signing secret proves Slack origin, not which saved
            # installation is entitled to receive or answer the event.
            raise ValueError("Slack webhook team does not match the connector credential.")
        return {"verified": True, "required": True, "provider": "slack", "credential_id": credential.id}

    def _verify_teams(self, *, headers: dict[str, str], body: bytes, credential: CredentialDefinition) -> dict[
        str, Any]:
        # Teams integration is intentionally permissive until a tenant-specific
        # verification scheme is configured for the installation. This keeps the
        # adapter boundary in place without blocking the backend-first channel path.
        secret = self._metadata_secret(credential, "webhook_secret_ref", "webhook_secret")
        if secret is None:
            self._verification_configuration_error(
                "Microsoft Teams credential metadata requires webhook_secret_ref.")
        signature = headers.get("x-ms-signature", "")
        if not signature:
            raise ValueError("Microsoft Teams webhook signature header is required.")
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Microsoft Teams webhook signature verification failed.")
        return {"verified": True, "required": True, "provider": "microsoft-teams", "credential_id": credential.id}

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

    def _verification_configuration_error(self, reason: str) -> NoReturn:
        raise ValueError(reason)


__all__ = ["ChannelWebhookVerificationService"]
