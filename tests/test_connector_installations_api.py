from __future__ import annotations

import asyncio
import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.domain import PublicEndpointRecord
from app.services.connector_installations import ConnectorInstallationService


class ConnectorInstallationsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))
        self.owner_headers = {
            "x-agency-user-id": "user-installations",
            "x-agency-user-email": "installations@example.com",
        }
        self.other_owner_headers = {
            "x-agency-user-id": "other-installations",
            "x-agency-user-email": "other-installations@example.com",
        }
        self.client.post(
            "/users/sync",
            json={
                "id": "user-installations",
                "email": "installations@example.com",
                "display_name": "Installations User",
            },
        )
        self.client.post(
            "/users/sync",
            json={
                "id": "other-installations",
                "email": "other-installations@example.com",
                "display_name": "Other Installations User",
            },
        )

        async def verified_ref(_service, installation):
            session_id = (installation.setup_session_id or installation.id).replace("-", "")[:12]
            return f"onecli://users/{installation.owner_user_id}/secrets/verified-{session_id}"

        self.verify_patcher = patch.object(
            ConnectorInstallationService,
            "_verified_onecli_ref",
            new=verified_ref,
        )
        self.verify_patcher.start()

    def tearDown(self) -> None:
        self.verify_patcher.stop()

    def _create_setup_session(self, provider: str = "telegram", headers: dict[str, str] | None = None) -> dict:
        response = self.client.post(
            f"/integrations/connectors/{provider}/setup-sessions",
            headers=headers or self.owner_headers,
            json={"name": f"{provider.title()} Connector"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_create_setup_session_returns_backend_owned_onecli_contract(self) -> None:
        body = self._create_setup_session()

        installation = body["installation"]
        installation_id = installation["id"]
        self.assertEqual(installation["provider"], "telegram-bot")
        self.assertEqual(installation["status"], "setup_pending")
        self.assertEqual(installation["owner_user_id"], "user-installations")
        self.assertEqual(
            body["onecli_credential_ref"],
            f"onecli://users/user-installations/telegram-bot/{installation_id}",
        )
        self.assertEqual(installation["onecli_credential_ref"], body["onecli_credential_ref"])
        self.assertEqual(urlparse(body["setup_url"]).query, "")
        self.assertEqual(urlparse(body["setup_url"]).path, "/")
        self.assertTrue(body["onecli_resource_name"].startswith("agency-telegram-bot-"))
        self.assertIsNotNone(body["expires_at"])
        self.assertIsNotNone(installation["setup_started_at"])
        self.assertIsNotNone(installation["setup_expires_at"])
        self.assertNotIn("/setup/connectors/", body["setup_url"])
        self.assertNotIn("raw-secret", str(body))

    def test_create_setup_session_accepts_planned_connector_without_health_implementation(self) -> None:
        response = self.client.post(
            "/integrations/connectors/gmail/setup-sessions",
            headers=self.owner_headers,
            json={"name": "Gmail"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["installation"]["provider"], "gmail")
        self.assertEqual(
            body["onecli_credential_ref"],
            f"onecli://users/user-installations/gmail/{body['installation']['id']}",
        )
        self.assertEqual(urlparse(body["setup_url"]).query, "")
        self.assertEqual(urlparse(body["setup_url"]).path, "/")
        self.assertNotIn("/setup/connectors/", body["setup_url"])

    def test_create_setup_session_rejects_unverifiable_multi_secret_shape(self) -> None:
        response = self.client.post(
            "/integrations/connectors/microsoft-teams/setup-sessions",
            headers=self.owner_headers,
            json={"name": "Teams"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot yet be verified", response.json()["detail"])

    def test_create_setup_session_rejects_raw_secret_material(self) -> None:
        response = self.client.post(
            "/integrations/connectors/telegram/setup-sessions",
            headers=self.owner_headers,
            json={"name": "Telegram", "token": "raw-secret"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("OneCLI setup", response.json()["detail"])

    def test_create_setup_session_rejects_browser_supplied_onecli_ref(self) -> None:
        response = self.client.post(
            "/integrations/connectors/telegram/setup-sessions",
            headers=self.owner_headers,
            json={
                "name": "Telegram",
                "onecli_credential_ref": "onecli://users/other-installations/secrets/forged",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("do not submit a credential ref", response.json()["detail"])

    def test_create_setup_session_rejects_secret_values_nested_in_metadata(self) -> None:
        response = self.client.post(
            "/integrations/connectors/whatsapp/setup-sessions",
            headers=self.owner_headers,
            json={"name": "WhatsApp", "metadata": {"app_secret": "raw-provider-secret"}},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("metadata accepts only secret references", response.json()["detail"])

    def test_create_setup_session_creates_unique_installations_per_request(self) -> None:
        first = self._create_setup_session("telegram")
        second_response = self.client.post(
            "/integrations/connectors/telegram/setup-sessions",
            headers=self.owner_headers,
            json={"name": "Telegram Bot Renamed", "metadata": {"channel": "ops"}},
        )
        self.assertEqual(second_response.status_code, 200, second_response.text)
        second = second_response.json()

        self.assertNotEqual(second["installation"]["id"], first["installation"]["id"])
        self.assertEqual(second["installation"]["name"], "Telegram Bot Renamed")
        self.assertEqual(second["installation"]["metadata"]["channel"], "ops")
        self.assertEqual(
            second["onecli_credential_ref"],
            f"onecli://users/user-installations/telegram-bot/{second['installation']['id']}",
        )

        list_response = self.client.get("/integrations/connectors/installations", headers=self.owner_headers)
        self.assertEqual(len(list_response.json()["items"]), 2)

    def test_installations_are_owner_isolated(self) -> None:
        body = self._create_setup_session()
        installation_id = body["installation"]["id"]

        other_get_response = self.client.get(
            f"/integrations/connectors/installations/{installation_id}",
            headers=self.other_owner_headers,
        )
        self.assertEqual(other_get_response.status_code, 404)

        owner_list_response = self.client.get(
            "/integrations/connectors/installations",
            headers=self.owner_headers,
        )
        other_list_response = self.client.get(
            "/integrations/connectors/installations",
            headers=self.other_owner_headers,
        )
        self.assertEqual(owner_list_response.status_code, 200)
        self.assertEqual(other_list_response.status_code, 200)
        self.assertEqual([item["id"] for item in owner_list_response.json()["items"]], [installation_id])
        self.assertEqual(other_list_response.json()["items"], [])

    def test_unexpired_setup_session_can_be_resumed_by_its_owner(self) -> None:
        body = self._create_setup_session("discord")
        installation_id = body["installation"]["id"]

        response = self.client.get(
            f"/integrations/connectors/installations/{installation_id}/setup-session",
            headers=self.owner_headers,
        )
        other_response = self.client.get(
            f"/integrations/connectors/installations/{installation_id}/setup-session",
            headers=self.other_owner_headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["onecli_resource_name"], body["onecli_resource_name"])
        self.assertEqual(response.json()["expires_at"], body["expires_at"])
        self.assertEqual(other_response.status_code, 404)

    def test_complete_setup_session_activates_installation_and_projects_credential_ref(self) -> None:
        body = self._create_setup_session("discord")
        installation_id = body["installation"]["id"]

        complete_response = self.client.post(
            f"/integrations/connectors/installations/{installation_id}/complete",
            headers=self.owner_headers,
            json={"metadata": {"workspace": "ops"}},
        )
        self.assertEqual(complete_response.status_code, 200, complete_response.text)
        completed = complete_response.json()
        self.assertEqual(completed["status"], "active")
        self.assertEqual(completed["provider"], "discord-bot")
        self.assertEqual(completed["metadata"]["workspace"], "ops")
        self.assertEqual(
            completed["onecli_credential_ref"],
            f"onecli://users/user-installations/secrets/verified-{installation_id.replace('-', '')[:12]}",
        )

        credential_response = self.client.get(f"/credentials/{installation_id}", headers=self.owner_headers)
        self.assertEqual(credential_response.status_code, 200, credential_response.text)
        credential = credential_response.json()
        self.assertEqual(credential["provider"], "discord-bot")
        self.assertNotIn("secret_ref", credential)
        self.assertTrue(credential["secret_ref_configured"])
        self.assertEqual(credential["secret_ref_scheme"], "onecli")
        self.assertEqual(credential["owner_user_id"], "user-installations")
        stored = asyncio.run(self.context.credential_repo.get(installation_id))
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.secret_ref, completed["onecli_credential_ref"])

    def test_complete_setup_session_rejects_browser_supplied_onecli_ref(self) -> None:
        body = self._create_setup_session("discord")
        installation_id = body["installation"]["id"]

        response = self.client.post(
            f"/integrations/connectors/installations/{installation_id}/complete",
            headers=self.owner_headers,
            json={"onecli_credential_ref": "onecli://users/other-installations/discord-bot/installation-2"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("do not submit a credential ref", response.json()["detail"])

    def test_complete_setup_session_rejects_non_onecli_ref(self) -> None:
        body = self._create_setup_session("discord")
        installation_id = body["installation"]["id"]

        response = self.client.post(
            f"/integrations/connectors/installations/{installation_id}/complete",
            headers=self.owner_headers,
            json={"onecli_credential_ref": "env://DISCORD_BOT_TOKEN"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("do not submit a credential ref", response.json()["detail"])

    def test_complete_setup_session_rejects_runtime_secret_mirror(self) -> None:
        body = self._create_setup_session("telegram")
        installation_id = body["installation"]["id"]

        response = self.client.post(
            f"/integrations/connectors/installations/{installation_id}/complete",
            headers=self.owner_headers,
            json={"runtime_secret_value": "telegram-token"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("do not submit a credential ref or secret", response.json()["detail"])

    def test_complete_setup_session_rejects_expired_session(self) -> None:
        body = self._create_setup_session("discord")
        installation_id = body["installation"]["id"]
        asyncio.run(
            self.context.connector_installation_repo.update(
                installation_id,
                {"setup_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
            )
        )

        # Use the real session guard while keeping the OneCLI transport mocked
        # by the class-level verifier for every other API test.
        async def verify_with_guard(service, installation):
            service._require_live_setup_session(installation)
            return "onecli://users/user-installations/secrets/never-used"

        with patch.object(ConnectorInstallationService, "_verified_onecli_ref", new=verify_with_guard):
            response = self.client.post(
                f"/integrations/connectors/installations/{installation_id}/complete",
                headers=self.owner_headers,
                json={},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.json()["detail"])

    def test_complete_setup_session_projects_verified_onecli_ref_for_telegram(self) -> None:
        body = self._create_setup_session("telegram")
        installation_id = body["installation"]["id"]

        complete_response = self.client.post(
            f"/integrations/connectors/installations/{installation_id}/complete",
            headers=self.owner_headers,
            json={
                "metadata": {"bot_user_id": "telegram-bot", "bot_username": "agency_bot"},
            },
        )

        self.assertEqual(complete_response.status_code, 200, complete_response.text)
        completed = complete_response.json()
        self.assertEqual(completed["status"], "active")
        self.assertEqual(completed["provider"], "telegram-bot")
        self.assertEqual(completed["metadata"]["bot_user_id"], "telegram-bot")
        self.assertEqual(completed["metadata"]["bot_username"], "agency_bot")

        credential_response = self.client.get(f"/credentials/{installation_id}", headers=self.owner_headers)
        self.assertEqual(credential_response.status_code, 200, credential_response.text)
        credential = credential_response.json()
        self.assertNotIn("secret_ref", credential)
        self.assertTrue(credential["secret_ref_configured"])
        self.assertEqual(credential["secret_ref_scheme"], "onecli")
        self.assertEqual(credential["provider"], "telegram-bot")
        self.assertEqual(credential["owner_user_id"], "user-installations")
        stored = asyncio.run(self.context.credential_repo.get(installation_id))
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(
            stored.secret_ref,
            f"onecli://users/user-installations/secrets/verified-{installation_id.replace('-', '')[:12]}",
        )

    def test_complete_setup_session_registers_telegram_webhook_automatically(self) -> None:
        body = self._create_setup_session("telegram")
        installation_id = body["installation"]["id"]

        class FakeTelegramResponse:
            status_code = 200
            text = '{"ok": true}'

            def json(self) -> dict[str, object]:
                return {"ok": True, "result": {"url": "registered"}}

        class FakeTelegramClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def __aenter__(self) -> "FakeTelegramClient":
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def post(self, url: str, data: dict[str, object] | None = None):
                self.calls.append((url, dict(data or {})))
                return FakeTelegramResponse()

        fake_client = FakeTelegramClient()
        with patch(
            "app.services.connector_installations.get_settings",
            return_value=SimpleNamespace(agency_public_webhook_base_url="https://decline-busboy-flogging.ngrok-free.dev"),
        ), patch(
            "app.services.connector_installations.ConnectorService.onecli_proxy_kwargs_for_owner",
            new=AsyncMock(return_value={"proxy": "http://onecli-gateway:8612"}),
        ), patch("app.services.connector_installations.httpx.AsyncClient", return_value=fake_client) as mock_client:
            complete_response = self.client.post(
                f"/integrations/connectors/installations/{installation_id}/complete",
                headers=self.owner_headers,
                json={
                    "metadata": {"bot_user_id": "telegram-bot", "bot_username": "agency_bot"},
                },
            )

        self.assertEqual(complete_response.status_code, 200, complete_response.text)
        completed = complete_response.json()
        mock_client.assert_called_once_with(timeout=10.0, proxy="http://onecli-gateway:8612")
        self.assertEqual(len(fake_client.calls), 1)
        url, data = fake_client.calls[0]
        self.assertEqual(url, "https://api.telegram.org/botonecli-managed/setWebhook")
        self.assertEqual(
            data["url"],
            f"https://decline-busboy-flogging.ngrok-free.dev/integrations/conversations/adapters/telegram/webhook?credential_id={installation_id}",
        )
        self.assertIn("webhook_secret_token_sha256", completed["metadata"])
        self.assertNotIn("webhook_secret_token", completed["metadata"])
        self.assertIn("secret_token", data)
        self.assertEqual(
            hashlib.sha256(str(data["secret_token"]).encode("utf-8")).hexdigest(),
            completed["metadata"]["webhook_secret_token_sha256"],
        )

    def test_failed_telegram_webhook_registration_does_not_activate_installation(self) -> None:
        body = self._create_setup_session("telegram")
        installation_id = body["installation"]["id"]

        class FailedTelegramResponse:
            status_code = 401
            text = '{"ok": false}'

            def json(self) -> dict[str, object]:
                return {"ok": False, "description": "Unauthorized"}

        class FailedTelegramClient:
            async def __aenter__(self) -> "FailedTelegramClient":
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def post(self, url: str, data: dict[str, object] | None = None):
                return FailedTelegramResponse()

        with patch(
            "app.services.connector_installations.get_settings",
            return_value=SimpleNamespace(agency_public_webhook_base_url="https://agency.example"),
        ), patch(
            "app.services.connector_installations.ConnectorService.onecli_proxy_kwargs_for_owner",
            new=AsyncMock(return_value={"proxy": "http://onecli-gateway:8612"}),
        ), patch(
            "app.services.connector_installations.httpx.AsyncClient",
            return_value=FailedTelegramClient(),
        ):
            response = self.client.post(
                f"/integrations/connectors/installations/{installation_id}/complete",
                headers=self.owner_headers,
                json={"metadata": {"bot_user_id": "telegram-bot"}},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unauthorized", response.json()["detail"])
        stored = asyncio.run(self.context.connector_installation_repo.get(installation_id))
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "setup_pending")

    def test_complete_setup_session_enforces_required_connector_metadata(self) -> None:
        body = self._create_setup_session("whatsapp")
        installation_id = body["installation"]["id"]

        missing_metadata_response = self.client.post(
            f"/integrations/connectors/installations/{installation_id}/complete",
            headers=self.owner_headers,
            json={},
        )
        self.assertEqual(missing_metadata_response.status_code, 400)
        self.assertIn("phone_number_id", str(missing_metadata_response.json()["detail"]))

        complete_response = self.client.post(
            f"/integrations/connectors/installations/{installation_id}/complete",
            headers=self.owner_headers,
            json={"metadata": {"phone_number_id": "1234567890"}},
        )
        self.assertEqual(complete_response.status_code, 200, complete_response.text)
        self.assertEqual(complete_response.json()["status"], "active")

    def test_complete_telegram_setup_uses_stored_public_endpoint_when_env_is_empty(self) -> None:
        body = self._create_setup_session("telegram")
        installation_id = body["installation"]["id"]
        asyncio.run(
            self.context.public_endpoint_repo.create(
                PublicEndpointRecord(
                    endpoint_type="webhook_base_url",
                    provider="cloudflare",
                    url="https://agency-demo.trycloudflare.com",
                    source="test",
                )
            )
        )

        class FakeTelegramResponse:
            status_code = 200
            text = '{"ok": true}'

            def json(self) -> dict[str, object]:
                return {"ok": True}

        class FakeTelegramClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def __aenter__(self) -> "FakeTelegramClient":
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def post(self, url: str, data: dict[str, object] | None = None):
                self.calls.append((url, dict(data or {})))
                return FakeTelegramResponse()

        fake_client = FakeTelegramClient()
        with patch(
            "app.services.connector_installations.get_settings",
            return_value=SimpleNamespace(agency_public_webhook_base_url=""),
        ), patch(
            "app.services.connector_installations.ConnectorService.onecli_proxy_kwargs_for_owner",
            new=AsyncMock(return_value={"proxy": "http://onecli-gateway:8612"}),
        ), patch("app.services.connector_installations.httpx.AsyncClient", return_value=fake_client):
            response = self.client.post(
                f"/integrations/connectors/installations/{installation_id}/complete",
                headers=self.owner_headers,
                json={
                    "metadata": {"bot_user_id": "telegram-bot", "bot_username": "agency_bot"},
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(fake_client.calls), 1)
        _, data = fake_client.calls[0]
        self.assertEqual(
            data["url"],
            f"https://agency-demo.trycloudflare.com/integrations/conversations/adapters/telegram/webhook?credential_id={installation_id}",
        )

    def test_startup_reconciliation_re_registers_active_telegram_webhooks(self) -> None:
        body = self._create_setup_session("telegram")
        installation_id = body["installation"]["id"]
        complete_response = self.client.post(
            f"/integrations/connectors/installations/{installation_id}/complete",
            headers=self.owner_headers,
            json={
                "metadata": {"bot_user_id": "telegram-bot", "bot_username": "agency_bot"},
            },
        )
        self.assertEqual(complete_response.status_code, 200, complete_response.text)
        asyncio.run(
            self.context.public_endpoint_repo.create(
                PublicEndpointRecord(
                    endpoint_type="webhook_base_url",
                    provider="cloudflare",
                    url="https://restart-demo.trycloudflare.com",
                    source="test",
                )
            )
        )

        class FakeTelegramResponse:
            status_code = 200
            text = '{"ok": true}'

            def json(self) -> dict[str, object]:
                return {"ok": True}

        class FakeTelegramClient:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def __aenter__(self) -> "FakeTelegramClient":
                return self

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                return False

            async def post(self, url: str, data: dict[str, object] | None = None):
                self.calls.append((url, dict(data or {})))
                return FakeTelegramResponse()

        fake_client = FakeTelegramClient()
        with patch(
            "app.services.connector_installations.get_settings",
            return_value=SimpleNamespace(agency_public_webhook_base_url=""),
        ), patch(
            "app.services.connector_installations.ConnectorService.onecli_proxy_kwargs_for_owner",
            new=AsyncMock(return_value={"proxy": "http://onecli-gateway:8612"}),
        ), patch("app.services.connector_installations.httpx.AsyncClient", return_value=fake_client):
            result = asyncio.run(ConnectorInstallationService(self.context).reconcile_startup_integrations())

        self.assertEqual(result["telegram_webhooks_reconciled"], 1)
        self.assertEqual(result["telegram_webhook_errors"], 0)
        self.assertEqual(len(fake_client.calls), 1)
        _, data = fake_client.calls[0]
        self.assertEqual(
            data["url"],
            f"https://restart-demo.trycloudflare.com/integrations/conversations/adapters/telegram/webhook?credential_id={installation_id}",
        )

    def test_revoke_marks_installation_revoked(self) -> None:
        body = self._create_setup_session("discord")
        installation_id = body["installation"]["id"]
        completed = self.client.post(
            f"/integrations/connectors/installations/{installation_id}/complete",
            headers=self.owner_headers,
            json={},
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        active_credential = asyncio.run(self.context.credential_repo.get(installation_id))
        self.assertEqual(active_credential.status.value, "active")

        response = self.client.delete(
            f"/integrations/connectors/installations/{installation_id}",
            headers=self.owner_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "revoked")
        self.assertIsNotNone(response.json()["revoked_at"])
        revoked_credential = asyncio.run(self.context.credential_repo.get(installation_id))
        self.assertEqual(revoked_credential.status.value, "revoked")
        self.assertIsNotNone(revoked_credential.revoked_at)

    def test_rotate_creates_rotation_setup_session_and_complete_marks_rotated(self) -> None:
        body = self._create_setup_session("discord")
        installation_id = body["installation"]["id"]
        complete_response = self.client.post(
            f"/integrations/connectors/installations/{installation_id}/complete",
            headers=self.owner_headers,
            json={},
        )
        self.assertEqual(complete_response.status_code, 200, complete_response.text)

        rotate_response = self.client.post(
            f"/integrations/connectors/installations/{installation_id}/rotate",
            headers=self.owner_headers,
            json={},
        )
        self.assertEqual(rotate_response.status_code, 200, rotate_response.text)
        rotation = rotate_response.json()
        self.assertEqual(rotation["installation"]["id"], installation_id)
        self.assertEqual(rotation["installation"]["status"], "rotation_required")
        self.assertNotEqual(rotation["onecli_resource_name"], body["onecli_resource_name"])
        self.assertIsNotNone(rotation["expires_at"])

        rotated_response = self.client.post(
            f"/integrations/connectors/installations/{installation_id}/complete",
            headers=self.owner_headers,
            json={},
        )
        self.assertEqual(rotated_response.status_code, 200, rotated_response.text)
        rotated = rotated_response.json()
        self.assertEqual(rotated["status"], "active")
        self.assertIsNotNone(rotated["last_rotated_at"])

    def test_rotate_rejects_revoked_installation(self) -> None:
        body = self._create_setup_session("discord")
        installation_id = body["installation"]["id"]
        revoke_response = self.client.delete(
            f"/integrations/connectors/installations/{installation_id}",
            headers=self.owner_headers,
        )
        self.assertEqual(revoke_response.status_code, 200)

        rotate_response = self.client.post(
            f"/integrations/connectors/installations/{installation_id}/rotate",
            headers=self.owner_headers,
            json={},
        )
        self.assertEqual(rotate_response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
