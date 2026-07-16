from __future__ import annotations

import unittest
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app


class IntegrationsRegistryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))
        self.owner_headers = {
            "x-agency-user-id": "user-integrations",
            "x-agency-user-email": "integrations@example.com",
        }
        self.client.post(
            "/users/sync",
            json={"id": "user-integrations", "email": "integrations@example.com", "display_name": "Integrations User"},
        )

    def test_list_integration_categories_returns_canonical_payload(self) -> None:
        response = self.client.get("/integrations/categories", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertIn("categories", payload)
        self.assertIn("updated_at", payload)
        self.assertIsInstance(payload["categories"], list)
        self.assertGreaterEqual(len(payload["categories"]), 1)

        communications = next((item for item in payload["categories"] if item["id"] == "communications"), None)
        self.assertIsNotNone(communications)
        self.assertEqual(communications["name"], "Communications")
        self.assertIn("Telegram", communications["providers"])
        self.assertEqual(
            communications["providers"]["Telegram"]["backendKey"],
            "telegram-bot",
        )
        self.assertEqual(
            communications["providers"]["WhatsApp Cloud API"]["launchPriority"],
            "now",
        )
        self.assertNotIn("home-tools", {item["id"] for item in payload["categories"]})

    def test_list_connector_capabilities_returns_operational_contract(self) -> None:
        response = self.client.get("/integrations/connectors/capabilities", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertIn("connectors", payload)
        self.assertIn("updated_at", payload)
        self.assertIn("telegram-bot", payload["connectors"])
        self.assertIn("whatsapp-cloud-api", payload["connectors"])

        telegram = payload["connectors"]["telegram-bot"]
        self.assertEqual(telegram["displayName"], "Telegram")
        self.assertEqual(telegram["authModel"], "bot token")
        self.assertEqual(telegram["capabilitySurface"], "connector")
        self.assertEqual(telegram["moduleCapabilities"], [])
        self.assertEqual(telegram["dependsOnAgencyCapabilities"], [])
        self.assertEqual(telegram["onecliTransportMode"], "proxy")
        self.assertFalse(telegram["runtimeSecretRequired"])
        self.assertTrue(telegram["setupSupported"])
        self.assertEqual(telegram["onecliSecretProfile"]["injectionTarget"], "url_path")
        self.assertEqual(telegram["onecliSecretProfile"]["pathTemplate"], "/bot{value}")
        self.assertTrue(telegram["healthSupported"])
        self.assertEqual(telegram["supportedSecretRefSchemes"], ["onecli://", "env://", "env:"])
        self.assertIn("bot_user_id", [item["key"] for item in telegram["instanceIdentityMetadata"]])
        self.assertIn("chat_id", [item["key"] for item in telegram["targetScopeMetadata"]])
        self.assertEqual(
            telegram["onecliSetupGuide"]["storagePath"],
            "onecli://users/{agency_user_id}/telegram-bot/{agency_installation_id}",
        )
        self.assertEqual(telegram["onecliSetupGuide"]["fields"][0]["key"], "bot_token")
        self.assertTrue(telegram["onecliSetupGuide"]["fields"][0]["secret"])
        self.assertIn("bot_user_id", [item["key"] for item in telegram["onecliSetupGuide"]["fields"]])
        self.assertIn("installation status", telegram["onecliSetupGuide"]["agencyStores"])
        self.assertTrue(telegram["onecliSetupGuide"]["notes"])
        self.assertTrue(any("URL path" in note for note in telegram["onecliSetupGuide"]["notes"]))
        self.assertTrue(any("agency-generated id" in note.lower() for note in telegram["onecliSetupGuide"]["notes"]))

        discord = payload["connectors"]["discord-bot"]
        self.assertEqual(discord["onecliTransportMode"], "proxy")
        self.assertFalse(discord["runtimeSecretRequired"])
        self.assertTrue(
            any("interactions webhook endpoint" in note.lower() for note in discord["onecliSetupGuide"]["notes"])
        )
        self.assertTrue(
            any("public key" in note.lower() and "different values" in note.lower()
                for note in discord["onecliSetupGuide"]["notes"])
        )

        whatsapp = payload["connectors"]["whatsapp-cloud-api"]
        self.assertEqual(whatsapp["requiredMetadata"][0]["key"], "phone_number_id")
        self.assertIn("phone_number_id", whatsapp["requiredMetadata"][0]["description"])
        self.assertIn(
            "active",
            whatsapp["onecliSetupGuide"]["completionSignal"],
        )
        github = payload["connectors"]["github"]
        self.assertEqual(github["onecliAppId"], "github")
        self.assertTrue(github["setupSupported"])
        self.assertEqual(github["requiredMetadata"], [])
        self.assertIn("owner", [item["key"] for item in github["instanceIdentityMetadata"]])
        self.assertIn("repo", [item["key"] for item in github["targetScopeMetadata"]])
        self.assertIn("owner", [item["key"] for item in github["onecliSetupGuide"]["fields"]])

        teams = payload["connectors"]["microsoft-teams"]
        self.assertFalse(teams["setupSupported"])
        self.assertIn("OAuth-refresh", teams["setupBlockReason"])
        linear = payload["connectors"]["linear"]
        self.assertIsNone(linear["onecliAppId"])
        self.assertEqual(linear["onecliSecretProfile"]["hostPattern"], "api.linear.app")
        outlook = payload["connectors"]["outlook-email"]
        self.assertFalse(outlook["setupSupported"])

        self.assertNotIn("home-assistant", payload["connectors"])


if __name__ == "__main__":
    unittest.main()
