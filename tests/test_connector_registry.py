from __future__ import annotations

import unittest

from app.integrations.connectors import (
    connector_health_supported,
    connector_target_scope_metadata,
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
        self.assertEqual(validate_connector_metadata("discord-bot", {}), [])
        self.assertEqual(
            validate_connector_metadata("whatsapp-cloud-api", {"phone_number_id": "phone-1"}),
            [],
        )

    def test_webhook_metadata_is_documented_but_not_required_for_delivery_credentials(self) -> None:
        telegram = get_connector_definition("telegram-bot")
        discord = get_connector_definition("discord-bot")
        whatsapp = get_connector_definition("whatsapp-cloud-api")

        self.assertIn("webhook_secret_ref", [item.metadata_key for item in telegram.required_metadata])
        self.assertIn("webhook_public_key", [item.metadata_key for item in discord.required_metadata])
        self.assertIn("app_secret_ref", [item.metadata_key for item in whatsapp.required_metadata])
        self.assertFalse(
            next(item for item in telegram.required_metadata if item.metadata_key == "webhook_secret_ref")
            .required_for_credential
        )
        self.assertFalse(
            next(item for item in discord.required_metadata if item.metadata_key == "webhook_public_key")
            .required_for_credential
        )

    def test_connector_identity_metadata_documents_repeated_installation_scope(self) -> None:
        slack = get_connector_definition("slack-app")
        teams = get_connector_definition("microsoft-teams")
        github = get_connector_definition("github")
        s3 = get_connector_definition("s3")

        self.assertIn("workspace_id", [item.metadata_key for item in slack.instance_identity_metadata])
        self.assertIn("webhook_secret_ref", [item.metadata_key for item in teams.required_metadata])
        self.assertIn("owner", [item.metadata_key for item in github.instance_identity_metadata])
        self.assertIn("bucket", [item.metadata_key for item in s3.instance_identity_metadata])
        self.assertEqual(validate_connector_metadata("github", {}), [])

    def test_connector_target_scope_metadata_documents_runtime_targets(self) -> None:
        self.assertIn("channel_id", [item.metadata_key for item in connector_target_scope_metadata("discord")])
        self.assertIn("repo", [item.metadata_key for item in connector_target_scope_metadata("github")])
        self.assertIn("folder_id", [item.metadata_key for item in connector_target_scope_metadata("google-drive")])


if __name__ == "__main__":
    unittest.main()
