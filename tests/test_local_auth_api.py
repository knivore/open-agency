from __future__ import annotations

import asyncio
import unittest
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.domain import UserDefinition


class LocalAuthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.client = TestClient(create_app(context=self.context))

    def test_bootstrap_login_and_me_round_trip(self) -> None:
        bootstrap = self.client.post(
            "/auth/bootstrap",
            json={
                "email": "admin@example.com",
                "password": "change-me-123",
                "display_name": "Local Admin",
            },
        )
        self.assertEqual(bootstrap.status_code, 200)
        bootstrap_body = bootstrap.json()
        self.assertTrue(bootstrap_body["bootstrap_complete"])
        self.assertEqual(bootstrap_body["user"]["email"], "admin@example.com")
        self.assertEqual(bootstrap_body["user"]["roles"], ["admin"])
        self.assertNotIn("password_hash", str(bootstrap_body))

        login = self.client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "change-me-123"},
        )
        self.assertEqual(login.status_code, 200)
        login_body = login.json()
        self.assertTrue(login_body["access_token"].startswith("agt_"))
        self.assertEqual(login_body["user"]["email"], "admin@example.com")
        self.assertNotIn("password_hash", str(login_body))

        me = self.client.get(
            "/auth/me",
            headers={"authorization": f"Bearer {login_body['access_token']}"},
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], "admin@example.com")
        self.assertNotIn("password_hash", str(me.json()))

    def test_bootstrap_becomes_unavailable_after_first_admin(self) -> None:
        first = self.client.post(
            "/auth/bootstrap",
            json={"email": "admin@example.com", "password": "change-me-123"},
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            "/auth/bootstrap",
            json={"email": "another@example.com", "password": "another-pass-123"},
        )
        self.assertEqual(second.status_code, 409)
        self.assertIn("already exists", second.json()["detail"])

    def test_bootstrap_promotes_existing_dev_auth_user_without_losing_identity_metadata(self) -> None:
        existing = UserDefinition(
            id="dev-user",
            email="dev@example.com",
            display_name="Dev User",
            provider="dev-auth",
            provider_subject="dev-user",
            provider_account_id="dev@example.com",
            metadata={"auth_mode": "dev"},
        )
        asyncio.run(self.context.user_repo.create(existing))

        bootstrap = self.client.post(
            "/auth/bootstrap",
            json={
                "email": "dev@example.com",
                "password": "change-me",
                "display_name": "Local Admin",
            },
        )

        self.assertEqual(bootstrap.status_code, 200)
        body = bootstrap.json()["user"]
        self.assertEqual(body["id"], "dev-user")
        self.assertEqual(body["roles"], ["admin"])
        self.assertEqual(body["provider"], "dev-auth")

        synced = asyncio.run(
            self.context.user_repo.upsert_from_identity(
                UserDefinition(
                    id="dev-user",
                    email="dev@example.com",
                    display_name="Dev User",
                    provider="dev-auth",
                    provider_subject="dev-user",
                    provider_account_id="dev@example.com",
                    metadata={"auth_mode": "dev"},
                )
            )
        )
        self.assertEqual(synced.roles, ["admin"])
        self.assertEqual(synced.metadata["auth_mode"], "dev")
        self.assertIn("local_auth", synced.metadata)

        login = self.client.post(
            "/auth/login",
            json={"email": "dev@example.com", "password": "change-me"},
        )
        self.assertEqual(login.status_code, 200)

    def test_login_rejects_invalid_password(self) -> None:
        bootstrap = self.client.post(
            "/auth/bootstrap",
            json={"email": "admin@example.com", "password": "change-me-123"},
        )
        self.assertEqual(bootstrap.status_code, 200)

        login = self.client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "wrong-password"},
        )
        self.assertEqual(login.status_code, 401)
        self.assertEqual(login.json()["detail"], "Invalid email or password")

    def test_setup_status_reflects_local_admin_bootstrap_progress(self) -> None:
        initial = self.client.get("/setup/status")
        self.assertEqual(initial.status_code, 200)
        initial_body = initial.json()
        self.assertIn("no_users", initial_body["blockers"])
        self.assertIn("no_admin_user", initial_body["blockers"])
        self.assertTrue(initial_body["users"]["auth_bootstrap_supported"])

        bootstrap = self.client.post(
            "/auth/bootstrap",
            json={"email": "admin@example.com", "password": "change-me-123"},
        )
        self.assertEqual(bootstrap.status_code, 200)

        after = self.client.get("/setup/status")
        self.assertEqual(after.status_code, 200)
        after_body = after.json()
        self.assertNotIn("no_users", after_body["blockers"])
        self.assertNotIn("no_admin_user", after_body["blockers"])
        self.assertIn("no_model_profiles", after_body["blockers"])
        self.assertFalse(after_body["users"]["auth_bootstrap_supported"])
