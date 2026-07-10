from __future__ import annotations

import os
import unittest
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache


class UsersApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))

    def test_user_sync_search_get_and_me_round_trip(self) -> None:
        sync_response = self.client.post(
            "/users/sync",
            json={
                "id": "user-1",
                "email": "Owner@Example.com",
                "display_name": "Owner One",
                "avatar_url": "https://example.com/avatar.png",
                "provider": "nextauth",
                "provider_subject": "provider-subject-1",
                "roles": ["admin"],
                "metadata": {"source": "test"},
            },
        )
        self.assertEqual(sync_response.status_code, 200)
        self.assertEqual(sync_response.json()["id"], "user-1")
        self.assertEqual(sync_response.json()["email"], "owner@example.com")

        update_response = self.client.post(
            "/users/sync",
            json={
                "email": "owner@example.com",
                "display_name": "Owner Updated",
                "provider": "nextauth",
                "provider_subject": "provider-subject-1",
            },
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["id"], "user-1")
        self.assertEqual(update_response.json()["display_name"], "Owner Updated")

        search_response = self.client.get("/users", params={"email": "owner"})
        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(len(search_response.json()["items"]), 1)

        get_response = self.client.get("/users/user-1")
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json()["email"], "owner@example.com")

        me_response = self.client.get("/me", headers={"x-agency-user-email": "owner@example.com"})
        self.assertEqual(me_response.status_code, 200)
        self.assertEqual(me_response.json()["id"], "user-1")

    def test_me_requires_synced_identity(self) -> None:
        missing_identity_response = self.client.get("/me")
        self.assertEqual(missing_identity_response.status_code, 401)

        unsynced_response = self.client.get("/me", headers={"x-agency-user-email": "missing@example.com"})
        self.assertEqual(unsynced_response.status_code, 404)

    def test_me_falls_back_to_trusted_identity_when_bearer_token_is_invalid(self) -> None:
        self.client.post(
            "/users/sync",
            json={
                "id": "user-1",
                "email": "owner@example.com",
                "display_name": "Owner One",
            },
        )

        response = self.client.get(
            "/me",
            headers={
                "authorization": "Bearer not-an-agency-token",
                "x-agency-user-email": "owner@example.com",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "user-1")

    def test_optional_auth_route_ignores_invalid_bearer_token(self) -> None:
        response = self.client.get(
            "/agents",
            headers={
                "authorization": "Bearer not-an-agency-token",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])

    def test_me_rejects_disabled_user(self) -> None:
        sync_response = self.client.post(
            "/users/sync",
            json={
                "id": "user-disabled",
                "email": "disabled@example.com",
                "display_name": "Disabled User",
                "status": "disabled",
            },
        )
        self.assertEqual(sync_response.status_code, 200)

        me_response = self.client.get("/me", headers={"x-agency-user-email": "disabled@example.com"})
        self.assertEqual(me_response.status_code, 403)
        self.assertEqual(me_response.json()["detail"], "Current user is disabled")

    def test_sync_requires_internal_key_when_configured(self) -> None:
        with patch.dict(
                os.environ,
                {"AGENCY_INTERNAL_API_KEY": "trusted-key"},
                clear=False,
        ):
            reset_settings_cache()
            blocked_response = self.client.post(
                "/users/sync",
                json={
                    "id": "user-1",
                    "email": "owner@example.com",
                    "display_name": "Owner One",
                },
            )
            self.assertEqual(blocked_response.status_code, 403)

            allowed_response = self.client.post(
                "/users/sync",
                headers={"x-agency-internal-api-key": "trusted-key"},
                json={
                    "id": "user-1",
                    "email": "owner@example.com",
                    "display_name": "Owner One",
                },
            )
            self.assertEqual(allowed_response.status_code, 200)
        reset_settings_cache()


if __name__ == "__main__":
    unittest.main()
