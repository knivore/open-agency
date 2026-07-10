"""Canonical provider metadata for supported multichannel chat adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChatChannelSpec:
    channel_type: str
    connector_provider_key: str
    aliases: tuple[str, ...] = ()
    supports_webhook_verification: bool = True
    supports_threaded_delivery: bool = True
    supports_user_target_delivery: bool = True
    requires_user_target_delivery: bool = False
    supports_interactive_callbacks: bool = True


_CHAT_CHANNEL_SPECS: tuple[ChatChannelSpec, ...] = (
    ChatChannelSpec(
        channel_type="telegram",
        connector_provider_key="telegram-bot",
        aliases=("telegram-bot",),
    ),
    ChatChannelSpec(
        channel_type="discord",
        connector_provider_key="discord-bot",
        aliases=("discord-bot",),
    ),
    ChatChannelSpec(
        channel_type="whatsapp",
        connector_provider_key="whatsapp-cloud-api",
        aliases=("whatsapp-cloud-api", "meta-whatsapp"),
        supports_threaded_delivery=False,
        supports_user_target_delivery=True,
        requires_user_target_delivery=True,
    ),
    ChatChannelSpec(
        channel_type="slack",
        connector_provider_key="slack-app",
        aliases=("slack-app",),
    ),
    ChatChannelSpec(
        channel_type="microsoft-teams",
        connector_provider_key="microsoft-teams",
        aliases=("teams",),
        supports_user_target_delivery=False,
        requires_user_target_delivery=False,
    ),
)


def chat_channel_spec(provider: str) -> ChatChannelSpec | None:
    normalized = provider.strip().lower()
    for spec in _CHAT_CHANNEL_SPECS:
        if normalized == spec.channel_type or normalized in spec.aliases:
            return spec
    return None


def normalize_chat_channel_provider(provider: str) -> str | None:
    spec = chat_channel_spec(provider)
    return spec.channel_type if spec is not None else None


def chat_channel_types() -> tuple[str, ...]:
    return tuple(spec.channel_type for spec in _CHAT_CHANNEL_SPECS)


def chat_channel_connector_provider_key(provider: str) -> str | None:
    spec = chat_channel_spec(provider)
    return spec.connector_provider_key if spec is not None else None


def can_deliver_to_thread(provider: str) -> bool:
    spec = chat_channel_spec(provider)
    return bool(spec and spec.supports_threaded_delivery)


def can_deliver_to_user(provider: str) -> bool:
    spec = chat_channel_spec(provider)
    return bool(spec and spec.requires_user_target_delivery)


def supports_webhook_verification(provider: str) -> bool:
    spec = chat_channel_spec(provider)
    return bool(spec and spec.supports_webhook_verification)


def supports_interactive_callbacks(provider: str) -> bool:
    spec = chat_channel_spec(provider)
    return bool(spec and spec.supports_interactive_callbacks)


__all__ = [
    "ChatChannelSpec",
    "can_deliver_to_thread",
    "can_deliver_to_user",
    "chat_channel_connector_provider_key",
    "chat_channel_spec",
    "chat_channel_types",
    "normalize_chat_channel_provider",
    "supports_interactive_callbacks",
    "supports_webhook_verification",
]
