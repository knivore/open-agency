"""Shared protocol boundaries for multichannel conversation adapters."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ChatChannelTransportContract(Protocol):
    """Shared transport surface every chat adapter must expose.

    The conversation engine depends on this boundary so new channels can plug in
    without changing assistant behavior, approval handling, or outbound routing.
    """

    channel_type: str

    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...

    def parse_message(self, payload: dict[str, Any]) -> Any:
        ...

    def parse_approval_action(self, payload: dict[str, Any]) -> Any:
        ...

    def format_outbound_messages(self, outbound_messages: list[dict[str, Any]], *, target: Any) -> list[dict[str, Any]]:
        ...


@runtime_checkable
class ChatChannelOutboundFormatterContract(Protocol):
    """Shared formatter surface for channel-specific outbound payloads."""

    provider: str

    def format_messages(self, outbound_messages: list[dict[str, Any]], *, target: Any) -> list[dict[str, Any]]:
        ...


@runtime_checkable
class ChatChannelWebhookVerificationContract(Protocol):
    """Shared webhook verification surface for chat providers."""

    async def verify(
            self,
            *,
            provider: str,
            credential_id: str | None,
            headers: dict[str, str],
            body: bytes,
    ) -> dict[str, Any]:
        ...


__all__ = [
    "ChatChannelOutboundFormatterContract",
    "ChatChannelTransportContract",
    "ChatChannelWebhookVerificationContract",
]
