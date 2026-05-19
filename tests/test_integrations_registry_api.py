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
        self.assertTrue(telegram["healthSupported"])
        self.assertEqual(telegram["supportedSecretRefSchemes"], ["env://", "env:"])

        whatsapp = payload["connectors"]["whatsapp-cloud-api"]
        self.assertEqual(whatsapp["requiredMetadata"][0]["key"], "phone_number_id")
        self.assertIn("phone_number_id", whatsapp["requiredMetadata"][0]["description"])


if __name__ == "__main__":
    unittest.main()
