from __future__ import annotations

import asyncio
import unittest
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app


class CredentialsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))
        self.owner_headers = {
            "x-agency-user-id": "user-1",
            "x-agency-user-email": "owner@example.com",
        }
        self.other_owner_headers = {
            "x-agency-user-id": "user-2",
            "x-agency-user-email": "other@example.com",
        }
        self.client.post(
            "/users/sync",
            json={
                "id": "user-1",
                "email": "owner@example.com",
                "display_name": "Owner One",
            },
        )
        self.client.post(
            "/users/sync",
            json={
                "id": "user-2",
                "email": "other@example.com",
                "display_name": "Owner Two",
            },
        )

    def test_credential_reference_crud_round_trip(self) -> None:
        create_response = self.client.post(
            "/credentials",
            headers=self.owner_headers,
            json={
                "id": "credential-openai",
                "name": "OpenAI API Key",
                "provider": "openai",
                "secret_ref": "secret://agency/openai-api-key",
                "metadata": {"environment": "dev"},
            },
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(create_response.json()["secret_ref"], "secret://agency/openai-api-key")
        self.assertEqual(create_response.json()["owner_user_id"], "user-1")
        self.assertEqual(create_response.json()["status"], "active")

        list_response = self.client.get("/credentials", headers=self.owner_headers)
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.json()["items"]), 1)

        other_owner_list_response = self.client.get("/credentials", headers=self.other_owner_headers)
        self.assertEqual(other_owner_list_response.status_code, 200)
        self.assertEqual(len(other_owner_list_response.json()["items"]), 0)

        update_response = self.client.put(
            "/credentials/credential-openai",
            headers=self.owner_headers,
            json={
                "name": "OpenAI API Key Updated",
                "metadata": {"environment": "prod"},
            },
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["name"], "OpenAI API Key Updated")
        self.assertEqual(update_response.json()["metadata"]["environment"], "prod")

        revoke_response = self.client.post("/credentials/credential-openai/revoke", headers=self.owner_headers)
        self.assertEqual(revoke_response.status_code, 200)
        self.assertEqual(revoke_response.json()["status"], "revoked")
        self.assertIsNotNone(revoke_response.json()["revoked_at"])

        delete_response = self.client.delete("/credentials/credential-openai", headers=self.owner_headers)
        self.assertEqual(delete_response.status_code, 200)
        self.assertTrue(delete_response.json()["deleted"])

        missing_response = self.client.get("/credentials/credential-openai", headers=self.owner_headers)
        self.assertEqual(missing_response.status_code, 404)

    def test_revoking_onecli_credential_ref_disables_owner_mappings(self) -> None:
        mapping_response = self.client.post(
            "/onecli/identity-mappings",
            headers=self.owner_headers,
            json={
                "id": "mapping-onecli-credential-revoke",
                "name": "OneCLI Credential Revoke",
                "onecli_agent_id": "onecli-agent-credential-revoke",
                "agent_token_secret_ref": "env://ONECLI_CREDENTIAL_REVOKE_TOKEN",
            },
        )
        self.assertEqual(mapping_response.status_code, 200)

        credential_response = self.client.post(
            "/credentials",
            headers=self.owner_headers,
            json={
                "id": "credential-onecli-ref",
                "name": "OneCLI Ref",
                "provider": "slack",
                "secret_ref": "onecli://users/user-1/slack/bot-token",
            },
        )
        self.assertEqual(credential_response.status_code, 200)

        revoke_response = self.client.post("/credentials/credential-onecli-ref/revoke", headers=self.owner_headers)
        self.assertEqual(revoke_response.status_code, 200)
        self.assertEqual(revoke_response.json()["status"], "revoked")

        mapping = asyncio.run(
            self.context.onecli_identity_mapping_repo.get(
                "mapping-onecli-credential-revoke",
                include_deleted=True,
            )
        )
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.status.value, "disabled")

        actions = self.context.runtime_operations.snapshot_dict()["recent_actions"]
        disabled_actions = [
            item for item in actions
            if item["action"] == "onecli.identity_mapping.disabled"
            and item["mapping_id"] == "mapping-onecli-credential-revoke"
        ]
        self.assertTrue(disabled_actions)
        self.assertEqual(disabled_actions[-1]["reason"], "credential_revoked")
        self.assertEqual(disabled_actions[-1]["credential_id"], "credential-onecli-ref")
        self.assertNotIn("ONECLI_CREDENTIAL_REVOKE_TOKEN", str(disabled_actions))

    def test_connector_schema_and_validation_routes_expose_backend_requirements(self) -> None:
        schema_response = self.client.get(
            "/credentials/connectors/whatsapp/schema",
            headers=self.owner_headers,
        )
        self.assertEqual(schema_response.status_code, 200)
        self.assertEqual(schema_response.json()["backendKey"], "whatsapp-cloud-api")
        self.assertEqual(schema_response.json()["requiredMetadata"][0]["key"], "phone_number_id")

        validate_response = self.client.post(
            "/credentials/connectors/whatsapp/validate",
            headers=self.owner_headers,
            json={
                "name": "WhatsApp Cloud API",
                "secret_ref": "env://WHATSAPP_TOKEN",
                "metadata": {},
            },
        )
        self.assertEqual(validate_response.status_code, 200)
        self.assertFalse(validate_response.json()["valid"])
        self.assertIn("phone_number_id", validate_response.json()["errors"][0])

    def test_connector_specific_create_and_update_routes_apply_canonical_provider_rules(self) -> None:
        create_response = self.client.post(
            "/credentials/connectors/telegram",
            headers=self.owner_headers,
            json={
                "id": "credential-connector-route",
                "name": "Telegram Bot",
                "secret_ref": "env://TELEGRAM_BOT_TOKEN",
                "metadata": {"channel": "ops"},
            },
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(create_response.json()["provider"], "telegram-bot")

        update_response = self.client.put(
            "/credentials/credential-connector-route/connector",
            headers=self.owner_headers,
            json={
                "provider": "whatsapp",
                "metadata": {"channel": "support", "phone_number_id": "1234567890"},
            },
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["provider"], "whatsapp-cloud-api")
        self.assertEqual(update_response.json()["metadata"]["phone_number_id"], "1234567890")

    def test_connector_resolver_matches_repeated_installations_by_identity_metadata(self) -> None:
        for credential_id, owner, repo in (
            ("credential-github-api", "acme", "api"),
            ("credential-github-web", "acme", "web"),
            ("credential-github-other", "other", "api"),
        ):
            response = self.client.post(
                "/credentials/connectors/github",
                headers=self.owner_headers,
                json={
                    "id": credential_id,
                    "name": f"GitHub {owner}/{repo}",
                    "secret_ref": f"env://{credential_id.upper().replace('-', '_')}",
                    "metadata": {"owner": owner, "repo": repo},
                },
            )
            self.assertEqual(response.status_code, 200)

        matched = self.client.post(
            "/credentials/connectors/resolve",
            headers=self.owner_headers,
            json={"provider": "github", "filters": {"owner": "acme", "repo": "api"}},
        )
        self.assertEqual(matched.status_code, 200)
        self.assertEqual(matched.json()["status"], "matched")
        self.assertEqual(matched.json()["credential"]["id"], "credential-github-api")
        self.assertIn("owner: acme", matched.json()["credential"]["identity_summary"])

        ambiguous = self.client.post(
            "/credentials/connectors/resolve",
            headers=self.owner_headers,
            json={"provider": "github", "filters": {"owner": "acme"}},
        )
        self.assertEqual(ambiguous.status_code, 200)
        self.assertEqual(ambiguous.json()["status"], "ambiguous")
        self.assertEqual(ambiguous.json()["match_count"], 2)

        cross_owner = self.client.post(
            "/credentials/connectors/resolve",
            headers=self.other_owner_headers,
            json={"provider": "github", "filters": {"owner": "acme", "repo": "api"}},
        )
        self.assertEqual(cross_owner.status_code, 200)
        self.assertEqual(cross_owner.json()["status"], "not_found")

    def test_connector_create_validation_rejects_raw_secret_payload_material(self) -> None:
        create_response = self.client.post(
            "/credentials/connectors/telegram",
            headers=self.owner_headers,
            json={
                "id": "credential-connector-raw-secret",
                "name": "Telegram Bot",
                "token": "raw-secret",
                "secret_ref": "env://TELEGRAM_BOT_TOKEN",
            },
        )
        self.assertEqual(create_response.status_code, 400)

    def test_connector_create_accepts_owner_scoped_onecli_reference(self) -> None:
        create_response = self.client.post(
            "/credentials/connectors/discord",
            headers=self.owner_headers,
            json={
                "id": "credential-discord-onecli-owner",
                "name": "Discord Bot OneCLI",
                "secret_ref": "onecli://users/user-1/discord/dev-bot",
            },
        )

        self.assertEqual(create_response.status_code, 200)
        body = create_response.json()
        self.assertEqual(body["provider"], "discord-bot")
        self.assertEqual(body["secret_ref"], "onecli://users/user-1/discord/dev-bot")
        self.assertEqual(body["owner_user_id"], "user-1")

    def test_connector_create_rejects_cross_owner_onecli_reference(self) -> None:
        validate_response = self.client.post(
            "/credentials/connectors/discord/validate",
            headers=self.owner_headers,
            json={
                "name": "Discord Bot OneCLI",
                "secret_ref": "onecli://users/user-2/discord/dev-bot",
            },
        )
        self.assertEqual(validate_response.status_code, 200)
        self.assertFalse(validate_response.json()["valid"])
        self.assertIn("onecli://users/user-1/", validate_response.json()["errors"][0])

        create_response = self.client.post(
            "/credentials/connectors/discord",
            headers=self.owner_headers,
            json={
                "id": "credential-discord-onecli-cross-owner",
                "name": "Discord Bot OneCLI",
                "secret_ref": "onecli://users/user-2/discord/dev-bot",
            },
        )

        self.assertEqual(create_response.status_code, 400)
        self.assertIn("onecli://users/user-1/", create_response.json()["detail"])

    def test_generic_credential_create_and_update_reject_cross_owner_onecli_reference(self) -> None:
        create_response = self.client.post(
            "/credentials",
            headers=self.owner_headers,
            json={
                "id": "credential-generic-onecli-cross-owner",
                "name": "Generic OneCLI Cross Owner",
                "provider": "slack",
                "secret_ref": "onecli://users/user-2/slack/bot-token",
            },
        )
        self.assertEqual(create_response.status_code, 400)
        self.assertIn("onecli://users/user-1/", create_response.json()["detail"])

        owner_create_response = self.client.post(
            "/credentials",
            headers=self.owner_headers,
            json={
                "id": "credential-generic-onecli-owner",
                "name": "Generic OneCLI Owner",
                "provider": "slack",
                "secret_ref": "onecli://users/user-1/slack/bot-token",
            },
        )
        self.assertEqual(owner_create_response.status_code, 200)

        update_response = self.client.put(
            "/credentials/credential-generic-onecli-owner",
            headers=self.owner_headers,
            json={"secret_ref": "onecli://users/user-2/slack/bot-token"},
        )
        self.assertEqual(update_response.status_code, 400)
        self.assertIn("onecli://users/user-1/", update_response.json()["detail"])

    def test_update_and_rotate_reject_cross_owner_onecli_reference(self) -> None:
        create_response = self.client.post(
            "/credentials/connectors/discord",
            headers=self.owner_headers,
            json={
                "id": "credential-discord-onecli-rotation",
                "name": "Discord Bot OneCLI",
                "secret_ref": "onecli://users/user-1/discord/dev-bot",
            },
        )
        self.assertEqual(create_response.status_code, 200)

        update_response = self.client.put(
            "/credentials/credential-discord-onecli-rotation/connector",
            headers=self.owner_headers,
            json={"secret_ref": "onecli://users/user-2/discord/dev-bot"},
        )
        self.assertEqual(update_response.status_code, 400)
        self.assertIn("onecli://users/user-1/", update_response.json()["detail"])

        rotate_response = self.client.post(
            "/credentials/credential-discord-onecli-rotation/rotate",
            headers=self.owner_headers,
            json={"secret_ref": "onecli://users/user-2/discord/dev-bot-v2"},
        )
        self.assertEqual(rotate_response.status_code, 400)
        self.assertIn("onecli://users/user-1/", rotate_response.json()["detail"])

    def test_connector_provider_aliases_are_normalized_on_create_and_update(self) -> None:
        create_response = self.client.post(
            "/credentials",
            headers=self.owner_headers,
            json={
                "id": "credential-telegram",
                "name": "Telegram Bot Token",
                "provider": "Telegram",
                "secret_ref": "secret://agency/telegram-bot-token",
                "metadata": {"channel": "ops"},
            },
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(create_response.json()["provider"], "telegram-bot")

        update_response = self.client.put(
            "/credentials/credential-telegram",
            headers=self.owner_headers,
            json={
                "provider": "meta-whatsapp",
                "metadata": {"channel": "support", "phone_number_id": "phone-number-1"},
            },
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["provider"], "whatsapp-cloud-api")

        get_response = self.client.get("/credentials/credential-telegram", headers=self.owner_headers)
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json()["provider"], "whatsapp-cloud-api")

        cross_owner_response = self.client.get("/credentials/credential-telegram", headers=self.other_owner_headers)
        self.assertEqual(cross_owner_response.status_code, 404)

        cross_owner_revoke_response = self.client.post(
            "/credentials/credential-telegram/revoke",
            headers=self.other_owner_headers,
        )
        self.assertEqual(cross_owner_revoke_response.status_code, 404)

    def test_whatsapp_credentials_require_phone_number_id_on_create(self) -> None:
        create_response = self.client.post(
            "/credentials",
            headers=self.owner_headers,
            json={
                "id": "credential-whatsapp-invalid",
                "name": "WhatsApp Cloud API",
                "provider": "whatsapp",
                "secret_ref": "env://WHATSAPP_TOKEN",
                "metadata": {},
            },
        )

        self.assertEqual(create_response.status_code, 422)
        self.assertIn("phone_number_id", str(create_response.json()["detail"]))

    def test_switching_to_whatsapp_requires_phone_number_id_on_update(self) -> None:
        create_response = self.client.post(
            "/credentials",
            headers=self.owner_headers,
            json={
                "id": "credential-generic",
                "name": "Generic Connector Token",
                "provider": "telegram",
                "secret_ref": "env://GENERIC_CONNECTOR_TOKEN",
                "metadata": {"channel": "ops"},
            },
        )
        self.assertEqual(create_response.status_code, 200)

        update_response = self.client.put(
            "/credentials/credential-generic",
            headers=self.owner_headers,
            json={
                "provider": "whatsapp",
                "metadata": {"channel": "support"},
            },
        )

        self.assertEqual(update_response.status_code, 422)
        self.assertIn("phone_number_id", str(update_response.json()["detail"]))

    def test_rotate_updates_reference_lifecycle_without_raw_secret_material(self) -> None:
        create_response = self.client.post(
            "/credentials",
            headers=self.owner_headers,
            json={
                "id": "credential-rotation",
                "name": "Rotating API Key",
                "provider": "openai",
                "secret_ref": "secret://agency/openai-api-key/v1",
                "metadata": {"environment": "dev"},
            },
        )
        self.assertEqual(create_response.status_code, 200)

        rotate_response = self.client.post(
            "/credentials/credential-rotation/rotate",
            headers=self.owner_headers,
            json={
                "secret_ref": "secret://agency/openai-api-key/v2",
                "metadata": {"rotated_by": "test"},
                "rotation_policy": {"interval_days": 90},
            },
        )
        self.assertEqual(rotate_response.status_code, 200)
        body = rotate_response.json()
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["secret_ref"], "secret://agency/openai-api-key/v2")
        self.assertEqual(body["secret_version"], 2)
        self.assertIsNotNone(body["last_rotated_at"])
        self.assertIsNone(body["revoked_at"])
        self.assertEqual(body["metadata"]["environment"], "dev")
        self.assertEqual(body["metadata"]["rotated_by"], "test")
        self.assertEqual(body["rotation_policy"]["interval_days"], 90)

        raw_secret_response = self.client.post(
            "/credentials/credential-rotation/rotate",
            headers=self.owner_headers,
            json={"token": "raw-secret-value"},
        )
        self.assertEqual(raw_secret_response.status_code, 400)

        cross_owner_response = self.client.post(
            "/credentials/credential-rotation/rotate",
            headers=self.other_owner_headers,
            json={},
        )
        self.assertEqual(cross_owner_response.status_code, 404)

    def test_rotate_reactivates_revoked_credential_reference(self) -> None:
        create_response = self.client.post(
            "/credentials",
            headers=self.owner_headers,
            json={
                "id": "credential-revoked-rotation",
                "name": "Revoked API Key",
                "provider": "openai",
                "secret_ref": "secret://agency/revoked-openai-api-key/v1",
            },
        )
        self.assertEqual(create_response.status_code, 200)

        revoke_response = self.client.post("/credentials/credential-revoked-rotation/revoke",
                                           headers=self.owner_headers)
        self.assertEqual(revoke_response.status_code, 200)
        self.assertEqual(revoke_response.json()["status"], "revoked")

        rotate_response = self.client.post(
            "/credentials/credential-revoked-rotation/rotate",
            headers=self.owner_headers,
            json={"secret_ref": "secret://agency/revoked-openai-api-key/v2"},
        )
        self.assertEqual(rotate_response.status_code, 200)
        self.assertEqual(rotate_response.json()["status"], "active")
        self.assertIsNone(rotate_response.json()["revoked_at"])


if __name__ == "__main__":
    unittest.main()
