from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.api.context import ApiContext
from app.domain import CredentialStatus
from app.integrations import normalize_connector_provider_key, resolve_secret_ref
from app.services.credentials import CredentialService


@dataclass(slots=True)
class ChannelOutboundDeliveryService:
    context: ApiContext

    async def deliver_for_owner(
        self,
        *,
        provider: str,
        credential_id: str,
        owner_user_id: str,
        provider_outbound_messages: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        credential = await CredentialService(self.context).get_credential_for_owner(credential_id, owner_user_id)
        if credential is None:
            return None

        expected_provider = self._expected_credential_provider(provider)
        if normalize_connector_provider_key(credential.provider) != expected_provider:
            raise ValueError(f"Credential '{credential_id}' is not configured for {provider}.")
        if credential.status != CredentialStatus.ACTIVE:
            raise ValueError(f"Credential '{credential_id}' is not active.")

        resolved = resolve_secret_ref(credential.secret_ref)
        if resolved.value is None:
            raise ValueError(resolved.error or "Credential secret could not be resolved.")

        deliveries = [
            self._deliver_one(
                provider=provider,
                token=resolved.value,
                credential_metadata=credential.metadata,
                provider_message=message,
            )
            for message in provider_outbound_messages
        ]
        return {
            "ok": all(item.get("ok") for item in deliveries),
            "provider": provider,
            "credential_id": credential.id,
            "deliveries": deliveries,
        }

    def _expected_credential_provider(self, provider: str) -> str | None:
        normalized = provider.strip().lower()
        if normalized == "telegram":
            return "telegram-bot"
        if normalized == "discord":
            return "discord-bot"
        if normalized == "whatsapp":
            return "whatsapp-cloud-api"
        raise ValueError(f"Unsupported chat channel delivery provider '{provider}'")

    def _deliver_one(
        self,
        *,
        provider: str,
        token: str,
        credential_metadata: dict[str, Any],
        provider_message: dict[str, Any],
    ) -> dict[str, Any]:
        request = self._request_for(
            provider=provider,
            token=token,
            credential_metadata=credential_metadata,
            provider_message=provider_message,
        )
        response = httpx.request(timeout=10.0, **request)
        payload = self._safe_json(response)
        ok = 200 <= response.status_code < 300
        return {
            "ok": ok,
            "method": provider_message.get("method"),
            "status_code": response.status_code,
            "response": payload,
            **({"error": self._error_from(provider, payload, response.status_code)} if not ok else {}),
        }

    def _request_for(
        self,
        *,
        provider: str,
        token: str,
        credential_metadata: dict[str, Any],
        provider_message: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = provider.strip().lower()
        method = provider_message.get("method")
        payload = provider_message.get("payload")
        if not isinstance(method, str) or not isinstance(payload, dict):
            raise ValueError("Provider outbound message must include string method and object payload.")

        if normalized == "telegram":
            return {
                "method": "POST",
                "url": f"https://api.telegram.org/bot{token}/{method}",
                "json": payload,
            }

        if normalized == "discord":
            if method != "createMessage":
                raise ValueError(f"Unsupported Discord outbound method '{method}'.")
            channel_id = payload.get("channel_id")
            if not channel_id:
                raise ValueError("Discord outbound payload requires channel_id.")
            discord_payload = {key: value for key, value in payload.items() if key != "channel_id"}
            return {
                "method": "POST",
                "url": f"https://discord.com/api/v10/channels/{channel_id}/messages",
                "headers": {"Authorization": f"Bot {token}"},
                "json": discord_payload,
            }

        if normalized == "whatsapp":
            if method != "messages":
                raise ValueError(f"Unsupported WhatsApp outbound method '{method}'.")
            phone_number_id = str(credential_metadata.get("phone_number_id") or "").strip()
            if not phone_number_id:
                raise ValueError("WhatsApp credential metadata requires phone_number_id.")
            api_version = str(credential_metadata.get("api_version") or "v20.0").strip()
            return {
                "method": "POST",
                "url": f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages",
                "headers": {"Authorization": f"Bearer {token}"},
                "json": payload,
            }

        raise ValueError(f"Unsupported chat channel delivery provider '{provider}'")

    def _safe_json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {"text": response.text}
        return payload if isinstance(payload, dict) else {"payload": payload}

    def _error_from(self, provider: str, payload: dict[str, Any], status_code: int) -> str:
        if isinstance(payload.get("description"), str):
            return payload["description"]
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(payload.get("message"), str):
            return payload["message"]
        return f"{provider} API returned HTTP {status_code}"


__all__ = ["ChannelOutboundDeliveryService"]
