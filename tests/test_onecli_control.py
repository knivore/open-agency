from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import httpx

from app.integrations.onecli_catalog import ONECLI_SECRET_PROFILE_BY_CONNECTOR
from app.services.onecli_control import OneCLIControlClient, OneCLIControlError


class OneCLIControlClientTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, payload: list[dict[str, object]], *, expected_path: str) -> OneCLIControlClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, expected_path)
            self.assertEqual(request.headers["Authorization"], "Bearer oc_test-control-key")
            return httpx.Response(200, json=payload)

        return OneCLIControlClient(
            api_url="http://onecli:7337",
            api_key="oc_test-control-key",
            transport=httpx.MockTransport(handler),
        )

    async def test_verify_url_path_secret_matches_session_name_profile_and_freshness(self) -> None:
        started_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        client = self._client(
            [
                {
                    "id": "secret-telegram",
                    "name": "agency-telegram-bot-session123",
                    "type": "generic",
                    "hostPattern": "api.telegram.org",
                    "pathPattern": "/bot*",
                    "injectionConfig": {"pathTemplate": "/bot{value}"},
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                }
            ],
            expected_path="/v1/secrets",
        )

        verified = await client.verify_secret(
            resource_name="agency-telegram-bot-session123",
            started_at=started_at,
            profile=ONECLI_SECRET_PROFILE_BY_CONNECTOR["telegram-bot"],
        )

        self.assertEqual(verified.id, "secret-telegram")
        self.assertEqual(verified.kind, "secrets")

    async def test_verify_secret_rejects_stale_or_profile_mismatched_resources(self) -> None:
        started_at = datetime.now(timezone.utc)
        client = self._client(
            [
                {
                    "id": "old-secret",
                    "name": "agency-telegram-bot-session123",
                    "type": "generic",
                    "hostPattern": "api.telegram.org",
                    "pathPattern": "/bot*",
                    "injectionConfig": {"pathTemplate": "/bot{value}"},
                    "createdAt": (started_at - timedelta(hours=1)).isoformat(),
                },
                {
                    "id": "wrong-host",
                    "name": "agency-telegram-bot-session123",
                    "type": "generic",
                    "hostPattern": "attacker.example",
                    "pathPattern": "/bot*",
                    "injectionConfig": {"pathTemplate": "/bot{value}"},
                    "createdAt": started_at.isoformat(),
                },
            ],
            expected_path="/v1/secrets",
        )

        with self.assertRaisesRegex(OneCLIControlError, "No matching OneCLI secret"):
            await client.verify_secret(
                resource_name="agency-telegram-bot-session123",
                started_at=started_at,
                profile=ONECLI_SECRET_PROFILE_BY_CONNECTOR["telegram-bot"],
            )

    async def test_verify_native_connection_uses_provider_filter_and_connected_at(self) -> None:
        started_at = datetime.now(timezone.utc) - timedelta(seconds=5)

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/connections")
            self.assertEqual(request.url.params["provider"], "github")
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "connection-github",
                        "provider": "github",
                        "status": "connected",
                        "connectedAt": datetime.now(timezone.utc).isoformat(),
                    }
                ],
            )

        client = OneCLIControlClient(
            api_url="http://onecli:7337",
            api_key="oc_test-control-key",
            transport=httpx.MockTransport(handler),
        )
        verified = await client.verify_connection(provider="github", started_at=started_at)

        self.assertEqual(verified.id, "connection-github")
        self.assertEqual(verified.kind, "connections")

    async def test_verification_errors_do_not_include_onecli_response_bodies(self) -> None:
        raw_secret = "must-not-leak"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": raw_secret})

        client = OneCLIControlClient(
            api_url="http://onecli:7337",
            api_key="oc_test-control-key",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(OneCLIControlError) as raised:
            await client.verify_connection(provider="github", started_at=datetime.now(timezone.utc))

        self.assertNotIn(raw_secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
