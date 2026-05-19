from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.api.context import ApiContext
from .channels import ConversationChannelService


@dataclass(slots=True)
class AdapterInboundMessage:
    channel_type: str
    channel_thread_id: str | None
    channel_user_id: str
    channel_display_name: str | None
    text: str
    external_message_id: str | None
    metadata: dict[str, Any]


@dataclass(slots=True)
class AdapterApprovalAction:
    channel_type: str
    channel_thread_id: str | None
    channel_user_id: str
    approval_request_id: str
    action: str
    reason: str | None


@dataclass(slots=True)
class ChatChannelAdapter:
    context: ApiContext
    channel_type: str

    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        message = self.parse_message(payload)
        if message is not None:
            result = await ConversationChannelService(self.context).handle_inbound_message(
                channel_type=message.channel_type,
                channel_thread_id=message.channel_thread_id,
                channel_user_id=message.channel_user_id,
                channel_display_name=message.channel_display_name,
                internal_user_id=None,
                text=message.text,
                response_mode="sync",
                message_id=message.external_message_id,
                content=None,
                metadata=message.metadata,
            )
            result["provider_outbound_messages"] = self.format_outbound_messages(
                result.get("outbound_messages", []),
                target=message,
            )
            return {
                "handled": True,
                "adapter": self.channel_type,
                "event_type": "message",
                **result,
            }

        approval = self.parse_approval_action(payload)
        if approval is not None:
            result = await ConversationChannelService(self.context).handle_approval_action(
                approval_request_id=approval.approval_request_id,
                action=approval.action,
                channel_type=approval.channel_type,
                channel_user_id=approval.channel_user_id,
                internal_user_id=None,
                reason=approval.reason,
            )
            result["provider_outbound_messages"] = self.format_outbound_messages(
                result.get("outbound_messages", []),
                target=approval,
            )
            return {
                "handled": True,
                "adapter": self.channel_type,
                "event_type": "approval_action",
                **result,
            }

        return {
            "handled": False,
            "adapter": self.channel_type,
            "event_type": "unsupported",
            "reason": "Webhook payload did not contain a supported message or approval callback.",
        }

    def parse_message(self, payload: dict[str, Any]) -> AdapterInboundMessage | None:
        raise NotImplementedError

    def parse_approval_action(self, payload: dict[str, Any]) -> AdapterApprovalAction | None:
        return None

    def format_outbound_messages(
        self,
        outbound_messages: list[dict[str, Any]],
        *,
        target: AdapterInboundMessage | AdapterApprovalAction,
    ) -> list[dict[str, Any]]:
        formatter = create_channel_outbound_formatter(self.channel_type)
        return formatter.format_messages(outbound_messages, target=target)

    def _approval_action_from_data(
        self,
        *,
        channel_thread_id: str | None,
        channel_user_id: str | None,
        data: str | None,
    ) -> AdapterApprovalAction | None:
        if not channel_user_id or not data:
            return None
        parts = [part.strip() for part in data.split(":") if part.strip()]
        if len(parts) != 3 or parts[0] != "approval" or parts[1] not in {"approve", "reject"}:
            return None
        return AdapterApprovalAction(
            channel_type=self.channel_type,
            channel_thread_id=channel_thread_id,
            channel_user_id=channel_user_id,
            approval_request_id=parts[2],
            action=parts[1],
            reason=f"{self.channel_type} callback",
        )


class TelegramChannelAdapter(ChatChannelAdapter):
    def __init__(self, context: ApiContext) -> None:
        super().__init__(context=context, channel_type="telegram")

    def parse_message(self, payload: dict[str, Any]) -> AdapterInboundMessage | None:
        message = payload.get("message") or payload.get("edited_message")
        if not isinstance(message, dict):
            return None
        text = message.get("text") or message.get("caption")
        if not isinstance(text, str) or not text.strip():
            return None
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        channel_user_id = str(sender.get("id") or chat.get("id") or "").strip()
        if not channel_user_id:
            return None
        message_id = message.get("message_id")
        update_id = payload.get("update_id")
        return AdapterInboundMessage(
            channel_type=self.channel_type,
            channel_thread_id=str(chat.get("id") or channel_user_id),
            channel_user_id=channel_user_id,
            channel_display_name=self._display_name(sender),
            text=text,
            external_message_id=f"telegram:{update_id}:{message_id}" if update_id or message_id else None,
            metadata={"adapter": "telegram", "update_id": update_id, "chat": chat, "from": sender},
        )

    def parse_approval_action(self, payload: dict[str, Any]) -> AdapterApprovalAction | None:
        callback = payload.get("callback_query")
        if not isinstance(callback, dict):
            return None
        sender = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        return self._approval_action_from_data(
            channel_thread_id=str(chat.get("id") or sender.get("id") or ""),
            channel_user_id=str(sender.get("id") or ""),
            data=callback.get("data"),
        )

    def _display_name(self, sender: dict[str, Any]) -> str | None:
        username = sender.get("username")
        first_name = sender.get("first_name")
        last_name = sender.get("last_name")
        if username:
            return str(username)
        return " ".join(str(part) for part in (first_name, last_name) if part) or None


class DiscordChannelAdapter(ChatChannelAdapter):
    def __init__(self, context: ApiContext) -> None:
        super().__init__(context=context, channel_type="discord")

    def parse_message(self, payload: dict[str, Any]) -> AdapterInboundMessage | None:
        author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            content = self._interaction_text(payload)
        if not isinstance(content, str) or not content.strip():
            return None
        user = self._interaction_user(payload) or author
        channel_user_id = str(user.get("id") or "").strip()
        if not channel_user_id:
            return None
        return AdapterInboundMessage(
            channel_type=self.channel_type,
            channel_thread_id=str(payload.get("channel_id") or payload.get("guild_id") or channel_user_id),
            channel_user_id=channel_user_id,
            channel_display_name=str(user.get("global_name") or user.get("username") or "") or None,
            text=content,
            external_message_id=f"discord:{payload.get('id')}" if payload.get("id") else None,
            metadata={"adapter": "discord", "guild_id": payload.get("guild_id")},
        )

    def parse_approval_action(self, payload: dict[str, Any]) -> AdapterApprovalAction | None:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        user = self._interaction_user(payload)
        return self._approval_action_from_data(
            channel_thread_id=str(payload.get("channel_id") or payload.get("guild_id") or ""),
            channel_user_id=str((user or {}).get("id") or ""),
            data=data.get("custom_id"),
        )

    def _interaction_user(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if isinstance(payload.get("member"), dict) and isinstance(payload["member"].get("user"), dict):
            return payload["member"]["user"]
        if isinstance(payload.get("user"), dict):
            return payload["user"]
        return None

    def _interaction_text(self, payload: dict[str, Any]) -> str | None:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        options = data.get("options") if isinstance(data.get("options"), list) else []
        for option in options:
            if isinstance(option, dict) and option.get("name") in {"text", "message", "prompt"}:
                value = option.get("value")
                if isinstance(value, str):
                    return value
        return None


class WhatsAppChannelAdapter(ChatChannelAdapter):
    def __init__(self, context: ApiContext) -> None:
        super().__init__(context=context, channel_type="whatsapp")

    def parse_message(self, payload: dict[str, Any]) -> AdapterInboundMessage | None:
        item = self._first_message_item(payload)
        if item is None:
            return None
        message, value = item
        text_payload = message.get("text") if isinstance(message.get("text"), dict) else {}
        text = text_payload.get("body")
        if not isinstance(text, str) or not text.strip():
            return None
        channel_user_id = str(message.get("from") or "").strip()
        if not channel_user_id:
            return None
        contact = self._contact_for(value, channel_user_id)
        metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
        phone_number_id = metadata.get("phone_number_id")
        return AdapterInboundMessage(
            channel_type=self.channel_type,
            channel_thread_id=str(phone_number_id or channel_user_id),
            channel_user_id=channel_user_id,
            channel_display_name=self._contact_name(contact),
            text=text,
            external_message_id=f"whatsapp:{message.get('id')}" if message.get("id") else None,
            metadata={"adapter": "whatsapp", "phone_number_id": phone_number_id},
        )

    def parse_approval_action(self, payload: dict[str, Any]) -> AdapterApprovalAction | None:
        item = self._first_message_item(payload)
        if item is None:
            return None
        message, value = item
        interactive = message.get("interactive") if isinstance(message.get("interactive"), dict) else {}
        button = interactive.get("button_reply") if isinstance(interactive.get("button_reply"), dict) else {}
        metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
        return self._approval_action_from_data(
            channel_thread_id=str(metadata.get("phone_number_id") or message.get("from") or ""),
            channel_user_id=str(message.get("from") or ""),
            data=button.get("id"),
        )

    def _first_message_item(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for entry in payload.get("entry", []) if isinstance(payload.get("entry"), list) else []:
            for change in entry.get("changes", []) if isinstance(entry, dict) and isinstance(entry.get("changes"), list) else []:
                value = change.get("value") if isinstance(change, dict) and isinstance(change.get("value"), dict) else {}
                messages = value.get("messages") if isinstance(value.get("messages"), list) else []
                for message in messages:
                    if isinstance(message, dict):
                        return message, value
        return None

    def _contact_for(self, value: dict[str, Any], wa_id: str) -> dict[str, Any] | None:
        contacts = value.get("contacts") if isinstance(value.get("contacts"), list) else []
        for contact in contacts:
            if isinstance(contact, dict) and str(contact.get("wa_id") or "") == wa_id:
                return contact
        return None

    def _contact_name(self, contact: dict[str, Any] | None) -> str | None:
        if not contact or not isinstance(contact.get("profile"), dict):
            return None
        name = contact["profile"].get("name")
        return str(name) if name else None


@dataclass(slots=True)
class ChannelOutboundFormatter:
    provider: str

    def format_messages(
        self,
        outbound_messages: list[dict[str, Any]],
        *,
        target: AdapterInboundMessage | AdapterApprovalAction,
    ) -> list[dict[str, Any]]:
        return [self.format_message(message, target=target) for message in outbound_messages]

    def format_message(
        self,
        message: dict[str, Any],
        *,
        target: AdapterInboundMessage | AdapterApprovalAction,
    ) -> dict[str, Any]:
        return {
            "method": "send",
            "payload": {
                "to": target.channel_user_id,
                "text": self._text(message),
            },
        }

    def _approval_callback_data(self, action: str, message: dict[str, Any]) -> str:
        return f"approval:{action}:{message.get('approval_request_id')}"

    def _text(self, message: dict[str, Any]) -> str:
        text = message.get("text")
        return str(text) if text is not None else ""


class TelegramOutboundFormatter(ChannelOutboundFormatter):
    def __init__(self) -> None:
        super().__init__(provider="telegram")

    def format_message(
        self,
        message: dict[str, Any],
        *,
        target: AdapterInboundMessage | AdapterApprovalAction,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "method": "sendMessage",
            "payload": {
                "chat_id": target.channel_thread_id or target.channel_user_id,
                "text": self._text(message),
            },
        }
        if message.get("type") == "approval":
            payload["payload"]["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {"text": "Approve", "callback_data": self._approval_callback_data("approve", message)},
                        {"text": "Reject", "callback_data": self._approval_callback_data("reject", message)},
                    ]
                ]
            }
        return payload


class DiscordOutboundFormatter(ChannelOutboundFormatter):
    def __init__(self) -> None:
        super().__init__(provider="discord")

    def format_message(
        self,
        message: dict[str, Any],
        *,
        target: AdapterInboundMessage | AdapterApprovalAction,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "method": "createMessage",
            "payload": {
                "channel_id": target.channel_thread_id,
                "content": self._text(message),
            },
        }
        if message.get("type") == "approval":
            payload["payload"]["components"] = [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "style": 3,
                            "label": "Approve",
                            "custom_id": self._approval_callback_data("approve", message),
                        },
                        {
                            "type": 2,
                            "style": 4,
                            "label": "Reject",
                            "custom_id": self._approval_callback_data("reject", message),
                        },
                    ],
                }
            ]
        return payload


class WhatsAppOutboundFormatter(ChannelOutboundFormatter):
    def __init__(self) -> None:
        super().__init__(provider="whatsapp")

    def format_message(
        self,
        message: dict[str, Any],
        *,
        target: AdapterInboundMessage | AdapterApprovalAction,
    ) -> dict[str, Any]:
        to = target.channel_user_id
        payload: dict[str, Any]
        if message.get("type") == "approval":
            payload = {
                "method": "messages",
                "payload": {
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "interactive",
                    "interactive": {
                        "type": "button",
                        "body": {"text": self._text(message)},
                        "action": {
                            "buttons": [
                                {
                                    "type": "reply",
                                    "reply": {
                                        "id": self._approval_callback_data("approve", message),
                                        "title": "Approve",
                                    },
                                },
                                {
                                    "type": "reply",
                                    "reply": {
                                        "id": self._approval_callback_data("reject", message),
                                        "title": "Reject",
                                    },
                                },
                            ]
                        },
                    },
                },
            }
        else:
            payload = {
                "method": "messages",
                "payload": {
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": self._text(message)},
                },
            }
        return payload


def create_channel_outbound_formatter(provider: str) -> ChannelOutboundFormatter:
    normalized = provider.strip().lower()
    if normalized == "telegram":
        return TelegramOutboundFormatter()
    if normalized == "discord":
        return DiscordOutboundFormatter()
    if normalized == "whatsapp":
        return WhatsAppOutboundFormatter()
    return ChannelOutboundFormatter(provider=normalized)


def create_chat_channel_adapter(context: ApiContext, provider: str) -> ChatChannelAdapter:
    normalized = provider.strip().lower()
    if normalized == "telegram":
        return TelegramChannelAdapter(context)
    if normalized == "discord":
        return DiscordChannelAdapter(context)
    if normalized == "whatsapp":
        return WhatsAppChannelAdapter(context)
    raise ValueError(f"Unsupported chat channel adapter '{provider}'")


__all__ = [
    "AdapterApprovalAction",
    "AdapterInboundMessage",
    "ChatChannelAdapter",
    "ChannelOutboundFormatter",
    "DiscordChannelAdapter",
    "DiscordOutboundFormatter",
    "TelegramChannelAdapter",
    "TelegramOutboundFormatter",
    "WhatsAppChannelAdapter",
    "WhatsAppOutboundFormatter",
    "create_channel_outbound_formatter",
    "create_chat_channel_adapter",
]
