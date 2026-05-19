from __future__ import annotations

import unittest

from app.integrations import (
    connector_health_supported,
    get_connector_definition,
    normalize_connector_provider_key,
    validate_connector_metadata,
)


class ConnectorRegistryTests(unittest.TestCase):
    def test_aliases_normalize_to_canonical_connector_key(self) -> None:
        self.assertEqual(normalize_connector_provider_key("Telegram"), "telegram-bot")
        self.assertEqual(normalize_connector_provider_key("meta-whatsapp"), "whatsapp-cloud-api")
        self.assertEqual(normalize_connector_provider_key("discord"), "discord-bot")

    def test_connector_definition_exposes_health_support(self) -> None:
        self.assertTrue(connector_health_supported("telegram-bot"))
        self.assertFalse(connector_health_supported("slack-app"))
        self.assertEqual(
            get_connector_definition("whatsapp-cloud-api").health_check.request.url_template,
            "https://graph.facebook.com/{api_version}/{phone_number_id}",
        )

    def test_connector_metadata_validation_is_registry_driven(self) -> None:
        self.assertEqual(validate_connector_metadata("telegram-bot", {}), [])
        self.assertEqual(
            validate_connector_metadata("whatsapp-cloud-api", {}),
            ["WhatsApp Cloud API credentials require metadata.phone_number_id."],
        )


if __name__ == "__main__":
    unittest.main()
