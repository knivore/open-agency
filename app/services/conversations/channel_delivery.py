"""Delivery helpers for provider-specific outbound multichannel messages."""

from __future__ import annotations

import httpx
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.api.context import ApiContext
from app.core.config import get_settings
from app.core.tls import direct_tls_verify
from app.domain import CredentialDefinition, CredentialStatus
from app.integrations.connectors import get_connector_definition, normalize_connector_provider_key
from app.integrations.onecli import build_onecli_proxy_url
from app.integrations.secrets import is_onecli_secret_ref, onecli_secret_identifier
from app.services.credentials import CredentialService
from app.services.onecli import OneCLIIdentityMappingService
from .channel_adapters import AdapterInboundMessage, create_channel_outbound_formatter
from .channel_registry import can_deliver_to_thread, can_deliver_to_user, chat_channel_types


@dataclass(slots=True)
class ChannelOutboundDeliveryService:
    context: ApiContext

    def _allows_onecli_header_proxy(self, provider: str) -> bool:
        normalized = normalize_connector_provider_key(provider)
        return normalized in {"discord-bot", "telegram-bot"}

    def _direct_tls_verify(self) -> str | bool | None:
        return direct_tls_verify()

    async def deliver_for_owner(
            self,
            *,
            provider: str,
            credential_id: str,
            owner_user_id: str,
            provider_outbound_messages: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        expected_provider = self._expected_credential_provider(provider)
        credential = await CredentialService(self.context).get_credential_for_owner(credential_id, owner_user_id)
        if credential is None:
            credential = await self._credential_from_active_installation(
                credential_id=credential_id,
                owner_user_id=owner_user_id,
                expected_provider=expected_provider,
            )
        if credential is None:
            return None

        if normalize_connector_provider_key(credential.provider) != expected_provider:
            raise ValueError(f"Credential '{credential_id}' is not configured for {provider}.")
        if credential.status != CredentialStatus.ACTIVE:
            raise ValueError(f"Credential '{credential_id}' is not active.")

        transport_mode = self._onecli_transport_mode(expected_provider)
        credential_mode = "direct"
        token = ""
        onecli_identifier: str | None = None
        if is_onecli_secret_ref(credential.secret_ref):
            onecli_identifier = onecli_secret_identifier(credential.secret_ref)
            if not onecli_identifier:
                raise ValueError("OneCLI credential ref is empty.")
            settings = get_settings()
            if not settings.onecli_enabled:
                raise ValueError("ONECLI_ENABLED is false.")
            if transport_mode == "proxy" or self._allows_onecli_header_proxy(expected_provider):
                credential_mode = "onecli"
            else:
                raise ValueError(
                    "Direct transport mode requires a runtime-resolvable secret ref; "
                    "use the Agency runtime secret mirror for direct delivery."
                )
        else:
            resolved = await CredentialService(self.context).resolve_credential_secret(credential)
            if resolved.value is None:
                raise ValueError(resolved.error or "Credential secret could not be resolved.")
            token = resolved.value

        onecli_proxy_kwargs: dict[str, Any] | None = None
        if credential_mode == "onecli":
            onecli_proxy_kwargs = await self._onecli_proxy_kwargs_for_owner(owner_user_id)

        deliveries = [
            self._deliver_one(
                provider=provider,
                token=token,
                credential_metadata=credential.metadata,
                provider_message=message,
                credential_mode=credential_mode,
                onecli_proxy_kwargs=onecli_proxy_kwargs,
            )
            for message in provider_outbound_messages
        ]
        result = {
            "ok": all(item.get("ok") for item in deliveries),
            "provider": provider,
            "credential_id": credential.id,
            "credential_mode": credential_mode,
            "deliveries": deliveries,
        }
        if onecli_identifier is not None:
            result.update(
                {
                    "secret_source": "onecli",
                    "secret_identifier": onecli_identifier,
                    **self._onecli_metadata(onecli_identifier),
                }
            )
        return result

    async def _credential_from_active_installation(
            self,
            *,
            credential_id: str,
            owner_user_id: str,
            expected_provider: str,
    ) -> CredentialDefinition | None:
        installation = await self.context.connector_installation_repo.get(credential_id)
        if installation is None or installation.owner_user_id != owner_user_id:
            return None
        if normalize_connector_provider_key(installation.provider) != expected_provider:
            return None
        if installation.status != "active":
            raise ValueError(f"Connector installation '{credential_id}' is not active.")

        # Connector setup owns the installation record, while delivery still
        # consumes legacy credential rows. Recreate the projection lazily so old
        # active installations keep working after projection drift or cleanup.
        secret_ref = (
            f"secret://agency/installations/{installation.id}"
            if installation.runtime_secret_encrypted
            else installation.onecli_credential_ref
        )
        credential = CredentialDefinition(
            id=installation.id,
            owner_user_id=installation.owner_user_id,
            name=installation.name,
            provider=installation.provider,
            secret_ref=secret_ref,
            status=CredentialStatus.ACTIVE,
            metadata=installation.metadata,
        )
        if hasattr(self.context.credential_repo, "save"):
            return await self.context.credential_repo.save(credential)
        return await self.context.credential_repo.create(credential)

    async def deliver_conversation_messages_for_owner(
            self,
            *,
            conversation_id: str,
            credential_id: str,
            owner_user_id: str,
            outbound_messages: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        conversation = await self.context.conversation_repo.get(conversation_id)
        if conversation is None:
            return None
        provider = conversation.channel_type.value
        if provider not in chat_channel_types():
            raise ValueError(f"Conversation '{conversation_id}' is not bound to a chat delivery provider.")
        self._validate_conversation_target(
            provider=provider,
            channel_thread_id=conversation.channel_thread_id,
            channel_user_id=conversation.channel_user_id,
        )
        target = AdapterInboundMessage(
            channel_type=provider,
            channel_thread_id=conversation.channel_thread_id,
            channel_user_id=conversation.channel_user_id or "",
            channel_display_name=conversation.channel_display_name,
            text="",
            external_message_id=None,
            metadata=conversation.metadata,
        )
        provider_outbound_messages = create_channel_outbound_formatter(provider).format_messages(
            outbound_messages,
            target=target,
        )
        result = await self.deliver_for_owner(
            provider=provider,
            credential_id=credential_id,
            owner_user_id=owner_user_id,
            provider_outbound_messages=provider_outbound_messages,
        )
        if result is not None:
            result["conversation_id"] = conversation.id
            result["target"] = {
                "channel_type": provider,
                "channel_thread_id": conversation.channel_thread_id,
                "channel_user_id": conversation.channel_user_id,
            }
        return result

    def _expected_credential_provider(self, provider: str) -> str | None:
        normalized = normalize_connector_provider_key(provider)
        if normalized in {
            "telegram-bot",
            "discord-bot",
            "whatsapp-cloud-api",
            "slack-app",
            "microsoft-teams",
            "twilio-sms",
            "gmail",
            "outlook-email",
        }:
            return normalized
        raise ValueError(f"Unsupported chat channel delivery provider '{provider}'")

    def _onecli_transport_mode(self, provider: str) -> str:
        definition = get_connector_definition(provider)
        if definition is None:
            return "proxy"
        return definition.onecli_transport_mode

    def _validate_conversation_target(
            self,
            *,
            provider: str,
            channel_thread_id: str | None,
            channel_user_id: str | None,
    ) -> None:
        if can_deliver_to_thread(provider) and not str(channel_thread_id or "").strip():
            raise ValueError(f"{provider.title()} delivery requires conversation.channel_thread_id.")
        if can_deliver_to_user(provider) and not str(channel_user_id or "").strip():
            raise ValueError("WhatsApp delivery requires conversation.channel_user_id.")

    def _deliver_one(
            self,
            *,
            provider: str,
            token: str,
            credential_metadata: dict[str, Any],
            provider_message: dict[str, Any],
            credential_mode: str = "direct",
            onecli_proxy_kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = self._request_for(
            provider=provider,
            token=token,
            credential_metadata=credential_metadata,
            provider_message=provider_message,
            credential_mode=credential_mode,
            onecli_proxy_kwargs=onecli_proxy_kwargs,
        )
        response = httpx.request(timeout=10.0, **request)
        payload = self._safe_json(response)
        ok = 200 <= response.status_code < 300
        return {
            "ok": ok,
            "method": provider_message.get("method"),
            "credential_mode": credential_mode,
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
            credential_mode: str = "direct",
            onecli_proxy_kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = normalize_connector_provider_key(provider) or provider.strip().lower()
        method = provider_message.get("method")
        payload = provider_message.get("payload")
        if not isinstance(method, str) or not isinstance(payload, dict):
            raise ValueError("Provider outbound message must include string method and object payload.")
        request_kwargs: dict[str, Any] = {}
        if credential_mode == "onecli":
            request_kwargs.update(onecli_proxy_kwargs or self._onecli_proxy_kwargs(None))
        else:
            # Direct deliveries must bypass inherited proxy env vars so Telegram
            # and other direct-capable connectors do not drift back through OneCLI.
            request_kwargs["trust_env"] = False
            verify = self._direct_tls_verify()
            if verify is not None:
                request_kwargs["verify"] = verify

        if normalized == "telegram-bot":
            # OneCLI v1.40+ replaces this placeholder with the bot token using
            # the configured `/bot{value}` URL-path injection template.
            effective_token = "onecli-managed" if credential_mode == "onecli" else token
            return {
                "method": "POST",
                "url": f"https://api.telegram.org/bot{effective_token}/{method}",
                "json": payload,
                **request_kwargs,
            }

        if normalized == "discord-bot":
            if method != "createMessage":
                raise ValueError(f"Unsupported Discord outbound method '{method}'.")
            channel_id = payload.get("channel_id")
            if not channel_id:
                raise ValueError("Discord outbound payload requires channel_id.")
            discord_payload = {key: value for key, value in payload.items() if key != "channel_id"}
            file_path = str(discord_payload.pop("file_path", "") or "").strip()
            filename = str(discord_payload.pop("filename", "") or "").strip()
            content_type = str(discord_payload.pop("content_type", "") or "").strip() or "application/octet-stream"
            if file_path:
                source_path = Path(file_path).expanduser().resolve()
                if not source_path.is_file():
                    raise ValueError(f"Discord attachment path is not a file: {source_path}.")
                filename = filename or source_path.name
                return {
                    "method": "POST",
                    "url": f"https://discord.com/api/v10/channels/{channel_id}/messages",
                    "headers": None if credential_mode == "onecli" else {"Authorization": f"Bot {token}"},
                    "data": {"payload_json": json.dumps(discord_payload)},
                    "files": {"files[0]": (filename, source_path.read_bytes(), content_type)},
                    **request_kwargs,
                }
            return {
                "method": "POST",
                "url": f"https://discord.com/api/v10/channels/{channel_id}/messages",
                "headers": None if credential_mode == "onecli" else {"Authorization": f"Bot {token}"},
                "json": discord_payload,
                **request_kwargs,
            }

        if normalized == "whatsapp-cloud-api":
            if method != "messages":
                raise ValueError(f"Unsupported WhatsApp outbound method '{method}'.")
            phone_number_id = str(credential_metadata.get("phone_number_id") or "").strip()
            if not phone_number_id:
                raise ValueError("WhatsApp credential metadata requires phone_number_id.")
            api_version = str(credential_metadata.get("api_version") or "v20.0").strip()
            return {
                "method": "POST",
                "url": f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages",
                "headers": None if credential_mode == "onecli" else {"Authorization": f"Bearer {token}"},
                "json": payload,
                **request_kwargs,
            }

        if normalized == "slack-app":
            if method != "chat.postMessage":
                raise ValueError(f"Unsupported Slack outbound method '{method}'.")
            if not payload.get("channel"):
                raise ValueError("Slack outbound payload requires channel.")
            return {
                "method": "POST",
                "url": "https://slack.com/api/chat.postMessage",
                "headers": None if credential_mode == "onecli" else {"Authorization": f"Bearer {token}"},
                "json": payload,
                **request_kwargs,
            }

        if normalized == "microsoft-teams":
            if method != "sendChannelMessage":
                raise ValueError(f"Unsupported Teams outbound method '{method}'.")
            team_id = str(payload.get("team_id") or credential_metadata.get("team_id") or "").strip()
            channel_id = str(payload.get("channel_id") or credential_metadata.get("channel_id") or "").strip()
            content = str(payload.get("content") or payload.get("text") or "").strip()
            attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else None
            if not team_id or not channel_id:
                raise ValueError("Teams outbound payload requires team_id and channel_id.")
            if not content and not attachments:
                raise ValueError("Teams outbound payload requires content.")
            request_body: dict[str, Any] = {
                "body": {
                    "contentType": str(payload.get("content_type") or "html"),
                    "content": content,
                }
            }
            if attachments:
                request_body["attachments"] = attachments
            return {
                "method": "POST",
                "url": f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}/messages",
                "headers": None if credential_mode == "onecli" else {"Authorization": f"Bearer {token}"},
                "json": request_body,
                **request_kwargs,
            }

        if normalized == "twilio-sms":
            if credential_mode == "onecli":
                raise ValueError("OneCLI channel delivery does not support Twilio basic-auth delivery yet.")
            if method != "messages":
                raise ValueError(f"Unsupported Twilio outbound method '{method}'.")
            account_sid = str(credential_metadata.get("account_sid") or payload.get("account_sid") or "").strip()
            from_number = str(payload.get("from") or credential_metadata.get("from_number") or "").strip()
            to_number = str(payload.get("to") or "").strip()
            body = str(payload.get("body") or payload.get("text") or "").strip()
            if not account_sid:
                raise ValueError("Twilio credential metadata requires account_sid.")
            if not from_number or not to_number or not body:
                raise ValueError("Twilio outbound payload requires from, to, and body.")
            return {
                "method": "POST",
                "url": f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
                "auth": (account_sid, token),
                "data": {"From": from_number, "To": to_number, "Body": body},
            }

        if normalized == "gmail":
            if method != "sendMessage":
                raise ValueError(f"Unsupported Gmail outbound method '{method}'.")
            user_id = str(payload.get("user_id") or credential_metadata.get("mailbox") or "me").strip()
            raw_message = str(payload.get("raw") or "").strip()
            if not raw_message:
                raise ValueError("Gmail outbound payload requires raw.")
            return {
                "method": "POST",
                "url": f"https://gmail.googleapis.com/gmail/v1/users/{user_id}/messages/send",
                "headers": None if credential_mode == "onecli" else {"Authorization": f"Bearer {token}"},
                "json": {"raw": raw_message},
                **request_kwargs,
            }

        if normalized == "outlook-email":
            if method != "sendMail":
                raise ValueError(f"Unsupported Outlook outbound method '{method}'.")
            mailbox = str(payload.get("mailbox") or credential_metadata.get("mailbox") or "").strip()
            message = payload.get("message")
            if not isinstance(message, dict):
                raise ValueError("Outlook outbound payload requires message.")
            path = f"users/{mailbox}/sendMail" if mailbox else "me/sendMail"
            return {
                "method": "POST",
                "url": f"https://graph.microsoft.com/v1.0/{path}",
                "headers": None if credential_mode == "onecli" else {"Authorization": f"Bearer {token}"},
                "json": {"message": message, "saveToSentItems": payload.get("saveToSentItems", True)},
                **request_kwargs,
            }

        raise ValueError(f"Unsupported chat channel delivery provider '{provider}'")

    def _onecli_proxy_kwargs(self, agent_token_secret_ref: str | None) -> dict[str, Any]:
        settings = get_settings()
        kwargs: dict[str, Any] = {
            "proxy": build_onecli_proxy_url(settings.onecli_gateway_url, agent_token_secret_ref),
        }
        if settings.onecli_gateway_ca_bundle_path:
            kwargs["verify"] = settings.onecli_gateway_ca_bundle_path
        return kwargs

    async def _onecli_proxy_kwargs_for_owner(self, owner_user_id: str) -> dict[str, Any]:
        token_context = await OneCLIIdentityMappingService(self.context).resolve_agent_token_context(
            owner_user_id=owner_user_id
        )
        agent_token_secret_ref = token_context.get("agent_token_secret_ref")
        if not isinstance(agent_token_secret_ref, str):
            agent_token_secret_ref = get_settings().onecli_agent_token_secret_ref
        return self._onecli_proxy_kwargs(agent_token_secret_ref)

    def _onecli_metadata(self, identifier: str) -> dict[str, Any]:
        settings = get_settings()
        return {
            "onecli": {
                "gateway_url": settings.onecli_gateway_url,
                "connection_ref": identifier,
                "agent_token_secret_ref_configured": bool(settings.onecli_agent_token_secret_ref),
            },
        }

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
