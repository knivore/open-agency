from __future__ import annotations

import unittest

from app.services.conversations.channel_registry import (
    can_deliver_to_thread,
    can_deliver_to_user,
    chat_channel_connector_provider_key,
    chat_channel_spec,
    chat_channel_types,
    normalize_chat_channel_provider,
)


class ChannelRegistryTests(unittest.TestCase):
    def test_registry_normalizes_channel_aliases(self) -> None:
        self.assertEqual(normalize_chat_channel_provider("slack-app"), "slack")
        self.assertEqual(normalize_chat_channel_provider("teams"), "microsoft-teams")
        self.assertEqual(chat_channel_connector_provider_key("slack"), "slack-app")
        self.assertEqual(chat_channel_connector_provider_key("microsoft-teams"), "microsoft-teams")
        self.assertIsNotNone(chat_channel_spec("teams"))
        self.assertIn("microsoft-teams", chat_channel_types())
        self.assertIn("slack", chat_channel_types())

    def test_registry_captures_delivery_shape(self) -> None:
        self.assertTrue(can_deliver_to_thread("discord"))
        self.assertTrue(can_deliver_to_thread("slack"))
        self.assertFalse(can_deliver_to_thread("whatsapp"))
        self.assertTrue(can_deliver_to_user("whatsapp"))
        self.assertFalse(can_deliver_to_user("discord"))


if __name__ == "__main__":
    unittest.main()
