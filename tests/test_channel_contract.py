from __future__ import annotations

import unittest

from app.api.context import create_test_api_context
from app.services.conversations.channel_adapters import create_channel_outbound_formatter, create_chat_channel_adapter
from app.services.conversations.channel_contract import (
    ChatChannelOutboundFormatterContract,
    ChatChannelTransportContract,
    ChatChannelWebhookVerificationContract,
)
from app.services.conversations.channel_webhooks import ChannelWebhookVerificationService


class ChannelContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()

    def test_supported_channel_adapters_satisfy_the_transport_contract(self) -> None:
        for provider in ("telegram", "discord", "whatsapp", "slack", "microsoft-teams"):
            adapter = create_chat_channel_adapter(self.context, provider)
            self.assertIsInstance(adapter, ChatChannelTransportContract)

    def test_supported_channel_formatters_satisfy_the_formatter_contract(self) -> None:
        for provider in ("telegram", "discord", "whatsapp", "slack", "microsoft-teams"):
            formatter = create_channel_outbound_formatter(provider)
            self.assertIsInstance(formatter, ChatChannelOutboundFormatterContract)

    def test_webhook_verifier_satisfies_the_verification_contract(self) -> None:
        verifier = ChannelWebhookVerificationService(self.context)
        self.assertIsInstance(verifier, ChatChannelWebhookVerificationContract)


if __name__ == "__main__":
    unittest.main()
