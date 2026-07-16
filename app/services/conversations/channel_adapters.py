"""Inbound/outbound adapter glue for multichannel conversation providers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.api.context import ApiContext
from .channel_contract import ChatChannelOutboundFormatterContract, ChatChannelTransportContract
from .channel_registry import normalize_chat_channel_provider
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
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatChannelAdapter(ChatChannelTransportContract):
    context: ApiContext
    channel_type: str

    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        approval = self.parse_approval_action(payload)
        if approval is not None:
            result = await ConversationChannelService(self.context).handle_approval_action(
                approval_request_id=approval.approval_request_id,
                action=approval.action,
                channel_type=approval.channel_type,
                channel_user_id=approval.channel_user_id,
                internal_user_id=None,
                reason=approval.reason,
                metadata=approval.metadata,
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
            metadata: dict[str, Any] | None = None,
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
            metadata=metadata or {},
        )

    def _channel_context(
            self,
            *,
            thread_id: str,
            user_id: str,
            display_name: str | None,
            extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Preserve a normalized transport snapshot so the main-agent can reason about
        # chat-native references without depending on browser-only page context.
        context: dict[str, Any] = {
            "channel_type": self.channel_type,
            "thread_id": thread_id,
            "user_id": user_id,
        }
        if display_name:
            context["display_name"] = display_name
        if extra:
            context.update({key: value for key, value in extra.items() if value not in (None, "", [], {})})
        return context


class TelegramChannelAdapter(ChatChannelAdapter):
    def __init__(self, context: ApiContext) -> None:
        super().__init__(context=context, channel_type="telegram")

    def parse_message(self, payload: dict[str, Any]) -> AdapterInboundMessage | None:
        message = payload.get("message") or payload.get("edited_message")
        callback = payload.get("callback_query") if isinstance(payload.get("callback_query"), dict) else None
        if isinstance(callback, dict):
            # Telegram callback queries can be the user's only visible interaction, so
            # preserve non-approval callbacks instead of dropping them on the floor.
            text = callback.get("data")
            if isinstance(text, str) and text.strip():
                sender = callback.get("from") if isinstance(callback.get("from"), dict) else {}
                channel_user_id = str(sender.get("id") or "").strip()
                if not channel_user_id:
                    return None
                update_id = payload.get("update_id")
                inline_message_id = callback.get("inline_message_id")
                callback_message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
                chat = callback_message.get("chat") if isinstance(callback_message.get("chat"), dict) else {}
                return AdapterInboundMessage(
                    channel_type=self.channel_type,
                    channel_thread_id=str(chat.get("id") or inline_message_id or channel_user_id),
                    channel_user_id=channel_user_id,
                    channel_display_name=self._display_name(sender),
                    text=text,
                    external_message_id=f"telegram:{update_id}:{callback.get('id')}" if update_id or callback.get(
                        "id") else None,
                    metadata={
                        "adapter": "telegram",
                        "update_id": update_id,
                        "callback_query": callback,
                        "channel_context": self._channel_context(
                            thread_id=str(chat.get("id") or inline_message_id or channel_user_id),
                            user_id=channel_user_id,
                            display_name=self._display_name(sender),
                            extra={
                                "inline_message_id": inline_message_id,
                                "callback_query_id": callback.get("id"),
                            },
                        ),
                    },
                )
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
        reply_to_message = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
        command_name, command_arguments = self._telegram_command_parts(text, message)
        return AdapterInboundMessage(
            channel_type=self.channel_type,
            channel_thread_id=str(chat.get("id") or channel_user_id),
            channel_user_id=channel_user_id,
            channel_display_name=self._display_name(sender),
            text=text,
            external_message_id=f"telegram:{update_id}:{message_id}" if update_id or message_id else None,
            metadata={
                "adapter": "telegram",
                "update_id": update_id,
                "chat": chat,
                "from": sender,
                "message_type": "edited_message" if payload.get("edited_message") is message else "message",
                "reply_to_message_id": reply_to_message.get("message_id") if reply_to_message else None,
                "reply_to_user_id": (
                    reply_to_message.get("from", {}).get("id")
                    if isinstance(reply_to_message.get("from"), dict)
                    else None
                ),
                "command_name": command_name,
                "command_arguments": command_arguments,
                "channel_context": self._channel_context(
                    thread_id=str(chat.get("id") or channel_user_id),
                    user_id=channel_user_id,
                    display_name=self._display_name(sender),
                    extra={
                        "chat": chat,
                        "reply_to_message_id": reply_to_message.get("message_id") if reply_to_message else None,
                        "command_name": command_name,
                    },
                ),
            },
        )

    def parse_approval_action(self, payload: dict[str, Any]) -> AdapterApprovalAction | None:
        callback = payload.get("callback_query")
        if not isinstance(callback, dict):
            return None
        sender = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        return self._approval_action_from_data(
            channel_thread_id=str(chat.get("id") or callback.get("inline_message_id") or sender.get("id") or ""),
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

    def _telegram_command_parts(self, text: str, message: dict[str, Any]) -> tuple[str | None, str | None]:
        entities = message.get("entities") if isinstance(message.get("entities"), list) else []
        if not text.startswith("/") or not entities:
            return None, None
        command_entities = [entity for entity in entities if
                            isinstance(entity, dict) and entity.get("type") == "bot_command"]
        if not command_entities:
            return None, None
        command_name = text.split()[0].lstrip("/")
        arguments = text[len(command_name) + 1:].strip()
        return command_name or None, arguments or None


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
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        message_reference = payload.get("message_reference") if isinstance(payload.get("message_reference"),
                                                                           dict) else {}
        thread_id = str(
            payload.get("channel_id")
            or message_reference.get("channel_id")
            or payload.get("guild_id")
            or channel_user_id
        )
        return AdapterInboundMessage(
            channel_type=self.channel_type,
            channel_thread_id=thread_id,
            channel_user_id=channel_user_id,
            channel_display_name=str(user.get("global_name") or user.get("username") or "") or None,
            text=content,
            external_message_id=f"discord:{payload.get('id')}" if payload.get("id") else None,
            metadata={
                "adapter": "discord",
                "guild_id": payload.get("guild_id"),
                "interaction_type": payload.get("type"),
                "interaction_name": data.get("name"),
                "interaction_custom_id": data.get("custom_id"),
                "interaction_component_type": data.get("component_type"),
                "reply_to_message_id": message_reference.get("message_id"),
                "edited_timestamp": payload.get("edited_timestamp"),
                "channel_context": self._channel_context(
                    thread_id=thread_id,
                    user_id=channel_user_id,
                    display_name=str(user.get("global_name") or user.get("username") or "") or None,
                    extra={
                        "guild_id": payload.get("guild_id"),
                        "reply_to_message_id": message_reference.get("message_id"),
                        "interaction_name": data.get("name"),
                    },
                ),
            },
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
        if not data:
            return None
        options = data.get("options") if isinstance(data.get("options"), list) else []
        for option in options:
            if isinstance(option, dict) and option.get("name") in {"text", "message", "prompt"}:
                value = option.get("value")
                if isinstance(value, str):
                    return value
        if isinstance(data.get("name"), str) and data.get("name").strip():
            serialized = self._serialize_interaction_options(options)
            return f"/{data['name'].strip()}{(' ' + serialized) if serialized else ''}"
        values = data.get("values") if isinstance(data.get("values"), list) else []
        if values:
            return " ".join(str(value).strip() for value in values if str(value).strip()) or None
        custom_id = data.get("custom_id")
        if isinstance(custom_id, str) and custom_id.strip():
            return custom_id.strip()
        return None

    def _serialize_interaction_options(self, options: list[Any]) -> str | None:
        parts: list[str] = []
        for option in options:
            if not isinstance(option, dict):
                continue
            name = str(option.get("name") or "").strip()
            if not name:
                continue
            value = option.get("value")
            if isinstance(value, list):
                value_text = ",".join(str(item).strip() for item in value if str(item).strip())
            elif value is None:
                value_text = ""
            else:
                value_text = str(value).strip()
            parts.append(f"{name}={value_text}" if value_text else name)
        return " ".join(parts) or None


class WhatsAppChannelAdapter(ChatChannelAdapter):
    def __init__(self, context: ApiContext) -> None:
        super().__init__(context=context, channel_type="whatsapp")

    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        status_callbacks = self._status_callbacks(payload)
        if status_callbacks:
            return {
                "handled": True,
                "adapter": self.channel_type,
                "event_type": "status_callback",
                "statuses": status_callbacks,
            }
        return await super().handle_webhook(payload)

    def parse_message(self, payload: dict[str, Any]) -> AdapterInboundMessage | None:
        item = self._first_message_item(payload)
        if item is None:
            return None
        message, value = item
        text = self._message_text(message)
        if not isinstance(text, str) or not text.strip():
            return None
        channel_user_id = str(message.get("from") or "").strip()
        if not channel_user_id:
            return None
        contact = self._contact_for(value, channel_user_id)
        metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
        phone_number_id = metadata.get("phone_number_id")
        message_type = self._message_type(message)
        context = message.get("context") if isinstance(message.get("context"), dict) else {}
        return AdapterInboundMessage(
            channel_type=self.channel_type,
            channel_thread_id=str(phone_number_id or channel_user_id),
            channel_user_id=channel_user_id,
            channel_display_name=self._contact_name(contact),
            text=text,
            external_message_id=f"whatsapp:{message.get('id')}" if message.get("id") else None,
            metadata={
                "adapter": "whatsapp",
                "phone_number_id": phone_number_id,
                "message_type": message_type,
                "reply_to_message_id": context.get("id"),
                "channel_context": self._channel_context(
                    thread_id=str(phone_number_id or channel_user_id),
                    user_id=channel_user_id,
                    display_name=self._contact_name(contact),
                    extra={
                        "phone_number_id": phone_number_id,
                        "reply_to_message_id": context.get("id"),
                        "message_type": message_type,
                    },
                ),
            },
        )

    def parse_approval_action(self, payload: dict[str, Any]) -> AdapterApprovalAction | None:
        item = self._first_message_item(payload)
        if item is None:
            return None
        message, value = item
        interactive = message.get("interactive") if isinstance(message.get("interactive"), dict) else {}
        button = interactive.get("button_reply") if isinstance(interactive.get("button_reply"), dict) else {}
        list_reply = interactive.get("list_reply") if isinstance(interactive.get("list_reply"), dict) else {}
        metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
        return self._approval_action_from_data(
            channel_thread_id=str(metadata.get("phone_number_id") or message.get("from") or ""),
            channel_user_id=str(message.get("from") or ""),
            data=button.get("id") or list_reply.get("id"),
        )

    def _first_message_item(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        for entry in payload.get("entry", []) if isinstance(payload.get("entry"), list) else []:
            for change in entry.get("changes", []) if isinstance(entry, dict) and isinstance(entry.get("changes"),
                                                                                             list) else []:
                value = change.get("value") if isinstance(change, dict) and isinstance(change.get("value"),
                                                                                       dict) else {}
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

    def _message_type(self, message: dict[str, Any]) -> str | None:
        for key in ("text", "image", "audio", "document", "video", "sticker", "location", "interactive", "button"):
            if isinstance(message.get(key), dict):
                return key
        return None

    def _message_text(self, message: dict[str, Any]) -> str | None:
        text_payload = message.get("text") if isinstance(message.get("text"), dict) else {}
        text = text_payload.get("body")
        if isinstance(text, str) and text.strip():
            return text
        if isinstance(message.get("caption"), str) and message.get("caption").strip():
            return str(message.get("caption")).strip()
        interactive = message.get("interactive") if isinstance(message.get("interactive"), dict) else {}
        button_reply = interactive.get("button_reply") if isinstance(interactive.get("button_reply"), dict) else {}
        if isinstance(button_reply.get("title"), str) and button_reply.get("title").strip():
            return str(button_reply.get("title")).strip()
        list_reply = interactive.get("list_reply") if isinstance(interactive.get("list_reply"), dict) else {}
        if isinstance(list_reply.get("title"), str) and list_reply.get("title").strip():
            return str(list_reply.get("title")).strip()
        message_type = self._message_type(message)
        if message_type:
            # Non-text WhatsApp payloads still need to be acknowledged so the webhook
            # contract remains consistent even when the assistant cannot act on media.
            return f"[whatsapp {message_type}]"
        return None

    def _status_callbacks(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        statuses: list[dict[str, Any]] = []
        for entry in payload.get("entry", []) if isinstance(payload.get("entry"), list) else []:
            for change in entry.get("changes", []) if isinstance(entry, dict) and isinstance(entry.get("changes"),
                                                                                             list) else []:
                value = change.get("value") if isinstance(change, dict) and isinstance(change.get("value"),
                                                                                       dict) else {}
                status_items = value.get("statuses") if isinstance(value.get("statuses"), list) else []
                for status in status_items:
                    if isinstance(status, dict):
                        statuses.append(status)
        return statuses


class TeamsChannelAdapter(ChatChannelAdapter):
    def __init__(self, context: ApiContext) -> None:
        super().__init__(context=context, channel_type="microsoft-teams")

    def parse_message(self, payload: dict[str, Any]) -> AdapterInboundMessage | None:
        if payload.get("type") not in {"message", "messageUpdate", "invoke"}:
            return None
        text = self._teams_text(payload)
        if not isinstance(text, str) or not text.strip():
            return None
        user = payload.get("from") if isinstance(payload.get("from"), dict) else {}
        conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
        channel_data = payload.get("channelData") if isinstance(payload.get("channelData"), dict) else {}
        channel_id = self._teams_channel_id(payload) or ""
        team_id = self._teams_team_id(payload)
        channel_user_id = str(user.get("id") or "").strip()
        if not channel_id or not channel_user_id:
            return None
        conversation_id = str(conversation.get("id") or channel_id or channel_user_id).strip()
        activity_id = payload.get("id")
        return AdapterInboundMessage(
            channel_type=self.channel_type,
            channel_thread_id=conversation_id,
            channel_user_id=channel_user_id,
            channel_display_name=str(user.get("name") or user.get("username") or "") or None,
            text=text,
            external_message_id=f"teams:{activity_id}" if isinstance(activity_id,
                                                                     str) and activity_id.strip() else None,
            metadata={
                "adapter": "microsoft-teams",
                "team_id": team_id,
                "channel_id": channel_id,
                "conversation": conversation,
                "channelData": channel_data,
                "channel_context": self._channel_context(
                    thread_id=conversation_id,
                    user_id=channel_user_id,
                    display_name=str(user.get("name") or user.get("username") or "") or None,
                    extra={
                        "team_id": team_id,
                        "channel_id": channel_id,
                        "conversation_id": conversation_id,
                    },
                ),
            },
        )

    def parse_approval_action(self, payload: dict[str, Any]) -> AdapterApprovalAction | None:
        if payload.get("type") not in {"invoke", "message", "messageUpdate"}:
            return None
        user = payload.get("from") if isinstance(payload.get("from"), dict) else {}
        conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
        channel_id = self._teams_channel_id(payload)
        channel_user_id = str(user.get("id") or "").strip()
        if not channel_id or not channel_user_id:
            return None
        value = payload.get("value")
        if isinstance(value, dict):
            data = (
                    value.get("approval")
                    or value.get("custom_id")
                    or value.get("action_id")
                    or value.get("id")
            )
            reason = value.get("comment") or value.get("reason")
        else:
            data = payload.get("text")
            reason = None
        if isinstance(data, dict):
            approval_action = str(data.get("approval_action") or data.get("action") or "").strip()
            approval_request_id = str(data.get("approval_request_id") or data.get("id") or "").strip()
            if approval_action in {"approve", "reject"} and approval_request_id:
                data = f"approval:{approval_action}:{approval_request_id}"
            elif approval_request_id and isinstance(data.get("value"), str) and data["value"].strip():
                data = data["value"].strip()
        approval = self._approval_action_from_data(
            channel_thread_id=str(conversation.get("id") or channel_id or channel_user_id),
            channel_user_id=channel_user_id,
            data=data if isinstance(data, str) else None,
            metadata={
                "team_id": self._teams_team_id(payload),
                "channel_id": channel_id,
                "conversation_id": conversation.get("id"),
            },
        )
        if approval is None:
            return None
        if isinstance(reason, str) and reason.strip():
            approval.reason = reason.strip()
        return approval

    def _teams_text(self, payload: dict[str, Any]) -> str | None:
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        content = body.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        value = payload.get("value") if isinstance(payload.get("value"), dict) else {}
        message = value.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        name = payload.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None

    def _teams_team_id(self, payload: dict[str, Any]) -> str | None:
        channel_data = payload.get("channelData") if isinstance(payload.get("channelData"), dict) else {}
        team = channel_data.get("team") if isinstance(channel_data.get("team"), dict) else {}
        if isinstance(team.get("id"), str) and team.get("id").strip():
            return team["id"].strip()
        if isinstance(channel_data.get("teamId"), str) and channel_data.get("teamId").strip():
            return str(channel_data.get("teamId")).strip()
        return None

    def _teams_channel_id(self, payload: dict[str, Any]) -> str | None:
        channel_data = payload.get("channelData") if isinstance(payload.get("channelData"), dict) else {}
        channel = channel_data.get("channel") if isinstance(channel_data.get("channel"), dict) else {}
        if isinstance(channel.get("id"), str) and channel.get("id").strip():
            return channel["id"].strip()
        if isinstance(channel_data.get("channelId"), str) and channel_data.get("channelId").strip():
            return str(channel_data.get("channelId")).strip()
        conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
        if isinstance(conversation.get("id"), str) and conversation.get("id").strip():
            return str(conversation.get("id")).strip()
        return None


class SlackChannelAdapter(ChatChannelAdapter):
    def __init__(self, context: ApiContext) -> None:
        super().__init__(context=context, channel_type="slack")

    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("type") == "url_verification":
            return {
                "handled": True,
                "adapter": self.channel_type,
                "event_type": "url_verification",
                "challenge": payload.get("challenge"),
            }
        return await super().handle_webhook(payload)

    def parse_message(self, payload: dict[str, Any]) -> AdapterInboundMessage | None:
        if isinstance(payload.get("command"), str) and payload.get("command").strip():
            return self._parse_command(payload)

        event = payload.get("event") if isinstance(payload.get("event"), dict) else None
        if not isinstance(event, dict):
            if payload.get("type") == "block_actions":
                return self._parse_action_message(payload)
            return None

        if event.get("type") not in {"message", "app_mention"} or event.get("subtype") in {"bot_message",
                                                                                           "message_changed",
                                                                                           "message_deleted"}:
            return None
        text = event.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        channel_id = str(event.get("channel") or payload.get("channel_id") or "").strip()
        channel_user_id = str(event.get("user") or event.get("bot_id") or "").strip()
        if not channel_id or not channel_user_id:
            return None
        thread_ts = str(event.get("thread_ts") or event.get("ts") or "").strip()
        event_id = payload.get("event_id")
        return AdapterInboundMessage(
            channel_type=self.channel_type,
            channel_thread_id=channel_id,
            channel_user_id=channel_user_id,
            channel_display_name=None,
            text=text,
            external_message_id=f"slack:{event_id}" if isinstance(event_id, str) and event_id.strip() else None,
            metadata={
                "adapter": "slack",
                "team_id": payload.get("team_id") or payload.get("api_app_id"),
                "channel_id": channel_id,
                "event_type": event.get("type"),
                "subtype": event.get("subtype"),
                "user_name": event.get("username"),
                "thread_ts": event.get("thread_ts"),
                "ts": event.get("ts"),
                "channel_context": self._channel_context(
                    thread_id=channel_id,
                    user_id=channel_user_id,
                    display_name=None,
                    extra={
                        "team_id": payload.get("team_id") or payload.get("api_app_id"),
                        "channel_id": channel_id,
                        "thread_ts": event.get("thread_ts"),
                    },
                ),
            },
        )

    def parse_approval_action(self, payload: dict[str, Any]) -> AdapterApprovalAction | None:
        if payload.get("type") != "block_actions":
            return None
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        channel_id = self._slack_channel_id(payload)
        container = payload.get("container") if isinstance(payload.get("container"), dict) else {}
        action = self._first_action(payload)
        if action is None or not channel_id:
            return None
        data = action.get("value") or action.get("action_id")
        return self._approval_action_from_data(
            channel_thread_id=channel_id,
            channel_user_id=str(user.get("id") or ""),
            data=data if isinstance(data, str) else None,
            metadata={
                "team_id": payload.get("team_id") or (
                    payload.get("team", {}).get("id") if isinstance(payload.get("team"), dict) else None
                ),
                "channel_id": channel_id,
                "thread_ts": container.get("message_ts") or payload.get("message_ts"),
            },
        )

    def _parse_command(self, payload: dict[str, Any]) -> AdapterInboundMessage | None:
        command = str(payload.get("command") or "").strip()
        text = str(payload.get("text") or "").strip()
        channel_id = str(payload.get("channel_id") or payload.get("channel_name") or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        if not command or not channel_id or not user_id:
            return None
        command_text = f"{command} {text}".strip()
        trigger_id = payload.get("trigger_id")
        return AdapterInboundMessage(
            channel_type=self.channel_type,
            channel_thread_id=channel_id,
            channel_user_id=user_id,
            channel_display_name=payload.get("user_name"),
            text=command_text,
            external_message_id=(
                f"slack:{trigger_id}" if isinstance(trigger_id,
                                                    str) and trigger_id.strip() else f"slack:{command}:{user_id}:{channel_id}"
            ),
            metadata={
                "adapter": "slack",
                "team_id": payload.get("team_id"),
                "channel_id": channel_id,
                "command": command,
                "response_url": payload.get("response_url"),
                "thread_ts": payload.get("message_ts") or payload.get("trigger_id"),
                "channel_context": self._channel_context(
                    thread_id=channel_id,
                    user_id=user_id,
                    display_name=payload.get("user_name"),
                    extra={"team_id": payload.get("team_id"), "command": command},
                ),
            },
        )

    def _parse_action_message(self, payload: dict[str, Any]) -> AdapterInboundMessage | None:
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        action = self._first_action(payload)
        channel_id = self._slack_channel_id(payload)
        if action is None or not channel_id:
            return None
        text = self._slack_action_text(action)
        if not text:
            return None
        trigger_id = payload.get("trigger_id")
        return AdapterInboundMessage(
            channel_type=self.channel_type,
            channel_thread_id=channel_id,
            channel_user_id=str(user.get("id") or ""),
            channel_display_name=str(user.get("username") or user.get("name") or "") or None,
            text=text,
            external_message_id=(
                f"slack:{trigger_id}" if isinstance(trigger_id, str) and trigger_id.strip() else None
            ),
            metadata={
                "adapter": "slack",
                "team_id": payload.get("team_id"),
                "channel_id": channel_id,
                "action_id": action.get("action_id"),
                "thread_ts": payload.get("message_ts"),
                "channel_context": self._channel_context(
                    thread_id=channel_id,
                    user_id=str(user.get("id") or ""),
                    display_name=str(user.get("username") or user.get("name") or "") or None,
                    extra={"team_id": payload.get("team_id"), "action_id": action.get("action_id")},
                ),
            },
        )

    def _first_action(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
        for action in actions:
            if isinstance(action, dict):
                return action
        return None

    def _slack_channel_id(self, payload: dict[str, Any]) -> str:
        container = payload.get("container") if isinstance(payload.get("container"), dict) else {}
        return str(
            payload.get("channel_id")
            or container.get("channel_id")
            or payload.get("channel")
            or ""
        ).strip()

    def _slack_action_text(self, action: dict[str, Any]) -> str | None:
        value = action.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip()
        action_id = action.get("action_id")
        if isinstance(action_id, str) and action_id.strip():
            return action_id.strip()
        text = action.get("text") if isinstance(action.get("text"), dict) else {}
        label = text.get("text") if isinstance(text.get("text"), str) else text.get("emoji")
        if isinstance(label, str) and label.strip():
            return label.strip()
        return None


@dataclass(slots=True)
class ChannelOutboundFormatter(ChatChannelOutboundFormatterContract):
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
        if isinstance(text, str) and text.strip():
            return text.strip()
        if text is not None and not isinstance(text, (dict, list)):
            return str(text)

        plain_text = message.get("plain_text")
        if isinstance(plain_text, str) and plain_text.strip():
            return plain_text.strip()

        for key in ("summary", "title", "body", "message", "description"):
            value = message.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        content = message.get("content")
        if isinstance(content, dict):
            for key in ("text", "plain_text", "summary", "title", "body", "description"):
                value = content.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            nested = content.get("result")
            if isinstance(nested, dict):
                for key in ("text", "plain_text", "summary", "title", "body", "description"):
                    value = nested.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            # Structured payloads should still produce a compact human-readable reply
            # rather than silently dropping to an empty transport message.
            return self._compact_text_from_mapping(content)

        return ""

    def _compact_text_from_mapping(self, data: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in ("message_type", "status", "action", "name", "type"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(f"{key}={value.strip()}")
        summary = data.get("summary")
        if isinstance(summary, str) and summary.strip():
            parts.append(summary.strip())
        if not parts:
            return "[structured message]"
        return " | ".join(parts)


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


class SlackOutboundFormatter(ChannelOutboundFormatter):
    def __init__(self) -> None:
        super().__init__(provider="slack")

    def format_message(
            self,
            message: dict[str, Any],
            *,
            target: AdapterInboundMessage | AdapterApprovalAction,
    ) -> dict[str, Any]:
        metadata = getattr(target, "metadata", None)
        channel_id = target.channel_thread_id or target.channel_user_id
        if isinstance(metadata, dict) and isinstance(metadata.get("channel_id"), str) and metadata[
            "channel_id"].strip():
            channel_id = metadata["channel_id"].strip()
        payload: dict[str, Any] = {
            "method": "chat.postMessage",
            "payload": {
                "channel": channel_id,
                "text": self._text(message),
            },
        }
        thread_ts = self._thread_ts(target)
        if thread_ts:
            payload["payload"]["thread_ts"] = thread_ts
        if message.get("type") == "approval":
            payload["payload"]["blocks"] = [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": self._text(message)},
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "style": "primary",
                            "text": {"type": "plain_text", "text": "Approve"},
                            "action_id": "approval_approve",
                            "value": self._approval_callback_data("approve", message),
                        },
                        {
                            "type": "button",
                            "style": "danger",
                            "text": {"type": "plain_text", "text": "Reject"},
                            "action_id": "approval_reject",
                            "value": self._approval_callback_data("reject", message),
                        },
                    ],
                },
            ]
        return payload

    def _thread_ts(self, target: AdapterInboundMessage | AdapterApprovalAction) -> str | None:
        metadata = getattr(target, "metadata", None)
        if isinstance(metadata, dict):
            thread_ts = metadata.get("thread_ts")
            if isinstance(thread_ts, str) and thread_ts.strip():
                return thread_ts.strip()
        return None


class TeamsOutboundFormatter(ChannelOutboundFormatter):
    def __init__(self) -> None:
        super().__init__(provider="microsoft-teams")

    def format_message(
            self,
            message: dict[str, Any],
            *,
            target: AdapterInboundMessage | AdapterApprovalAction,
    ) -> dict[str, Any]:
        metadata = getattr(target, "metadata", None)
        team_id = None
        channel_id = target.channel_thread_id or target.channel_user_id
        if isinstance(metadata, dict):
            if isinstance(metadata.get("team_id"), str) and metadata["team_id"].strip():
                team_id = metadata["team_id"].strip()
            if isinstance(metadata.get("channel_id"), str) and metadata["channel_id"].strip():
                channel_id = metadata["channel_id"].strip()
        content = self._text(message)
        payload: dict[str, Any] = {
            "method": "sendChannelMessage",
            "payload": {
                "team_id": team_id,
                "channel_id": channel_id,
                "content_type": "html",
                "content": content,
            },
        }
        if message.get("type") == "approval":
            approval_request_id = str(message.get("approval_request_id") or "").strip()
            card = self._approval_card(content=content, approval_request_id=approval_request_id)
            payload["payload"]["attachments"] = [
                {
                    "id": f"approval-card-{approval_request_id or 'pending'}",
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": json.dumps(card, separators=(",", ":")),
                }
            ]
            payload["payload"]["content"] = (
                f"{content}\n"
                f"Approve: approval:approve:{approval_request_id}\n"
                f"Reject: approval:reject:{approval_request_id}"
            ).strip()
        return payload

    def _approval_card(self, *, content: str, approval_request_id: str) -> dict[str, Any]:
        # Teams approvals are rendered as adaptive cards so the channel can offer
        # explicit action buttons while still preserving a plain-text fallback.
        return {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.5",
            "body": [
                {"type": "TextBlock", "text": content or "Approval required", "wrap": True},
                {
                    "type": "TextBlock",
                    "text": f"Request: {approval_request_id}" if approval_request_id else "Request pending",
                    "wrap": True,
                    "isSubtle": True,
                },
            ],
            "actions": [
                {
                    "type": "Action.Submit",
                    "title": "Approve",
                    "data": {
                        "approval": {
                            "approval_action": "approve",
                            "approval_request_id": approval_request_id,
                        }
                    },
                },
                {
                    "type": "Action.Submit",
                    "title": "Reject",
                    "data": {
                        "approval": {
                            "approval_action": "reject",
                            "approval_request_id": approval_request_id,
                        }
                    },
                },
            ],
        }


def create_channel_outbound_formatter(provider: str) -> ChatChannelOutboundFormatterContract:
    normalized = normalize_chat_channel_provider(provider) or provider.strip().lower()
    if normalized == "telegram":
        return TelegramOutboundFormatter()
    if normalized == "discord":
        return DiscordOutboundFormatter()
    if normalized == "whatsapp":
        return WhatsAppOutboundFormatter()
    if normalized == "slack":
        return SlackOutboundFormatter()
    if normalized == "microsoft-teams":
        return TeamsOutboundFormatter()
    return ChannelOutboundFormatter(provider=normalized)


def create_chat_channel_adapter(context: ApiContext, provider: str) -> ChatChannelTransportContract:
    normalized = normalize_chat_channel_provider(provider) or provider.strip().lower()
    if normalized == "telegram":
        return TelegramChannelAdapter(context)
    if normalized == "discord":
        return DiscordChannelAdapter(context)
    if normalized == "whatsapp":
        return WhatsAppChannelAdapter(context)
    if normalized == "slack":
        return SlackChannelAdapter(context)
    if normalized == "microsoft-teams":
        return TeamsChannelAdapter(context)
    raise ValueError(f"Unsupported chat channel adapter '{provider}'")


__all__ = [
    "AdapterApprovalAction",
    "AdapterInboundMessage",
    "ChatChannelAdapter",
    "ChannelOutboundFormatter",
    "DiscordChannelAdapter",
    "DiscordOutboundFormatter",
    "SlackChannelAdapter",
    "SlackOutboundFormatter",
    "TeamsChannelAdapter",
    "TeamsOutboundFormatter",
    "TelegramChannelAdapter",
    "TelegramOutboundFormatter",
    "WhatsAppChannelAdapter",
    "WhatsAppOutboundFormatter",
    "create_channel_outbound_formatter",
    "create_chat_channel_adapter",
]
