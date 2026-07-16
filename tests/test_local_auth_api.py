from __future__ import annotations

import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.domain import UserDefinition
from app.services.local_auth import (
    LocalAuthBootstrapUnavailableError,
    LocalAuthRateLimitError,
    LocalAuthService,
    _LOCAL_AUTH_FAILURES,
    _reserve_local_auth_attempt,
)


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
        self.assertTrue(bootstrap_body["user"]["local_credentials_enabled"])
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
        self.assertTrue(me.json()["local_credentials_enabled"])
        self.assertNotIn("password_hash", str(me.json()))

        conversations = self.client.get(
            "/conversations",
            headers={"authorization": f"Bearer {login_body['access_token']}"},
        )
        self.assertEqual(conversations.status_code, 200)

    def test_owner_can_update_local_email_and_password_and_must_sign_in_again(self) -> None:
        bootstrap = self.client.post(
            "/auth/bootstrap",
            json={"email": "admin@example.com", "password": "change-me-123"},
        )
        self.assertEqual(bootstrap.status_code, 200)
        login = self.client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "change-me-123"},
        )
        self.assertEqual(login.status_code, 200)
        session_token = login.json()["access_token"]
        session_headers = {"authorization": f"Bearer {session_token}"}

        automation = self.client.post(
            "/api-tokens",
            headers=session_headers,
            json={"name": "Owner script", "scopes": ["agents:read"]},
        )
        self.assertEqual(automation.status_code, 200)
        automation_token = automation.json()["token"]

        update = self.client.patch(
            "/auth/me/credentials",
            headers=session_headers,
            json={
                "email": "owner@example.com",
                "current_password": "change-me-123",
                "new_password": "friendlier-pass-456",
            },
        )

        self.assertEqual(update.status_code, 200)
        body = update.json()
        self.assertEqual(body["user"]["email"], "owner@example.com")
        self.assertTrue(body["reauthentication_required"])
        self.assertEqual(body["revoked_sessions"], 1)
        self.assertNotIn("password_hash", str(body))

        revoked_session = self.client.get("/auth/me", headers=session_headers)
        self.assertEqual(revoked_session.status_code, 401)
        preserved_automation = self.client.get(
            "/api-tokens/scopes",
            headers={"authorization": f"Bearer {automation_token}"},
        )
        self.assertEqual(preserved_automation.status_code, 200)

        old_login = self.client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "change-me-123"},
        )
        self.assertEqual(old_login.status_code, 401)
        new_login = self.client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": "friendlier-pass-456"},
        )
        self.assertEqual(new_login.status_code, 200)

    def test_existing_local_session_picks_up_new_catalog_scopes(self) -> None:
        self.client.post(
            "/auth/bootstrap",
            json={"email": "admin@example.com", "password": "change-me-123"},
        )
        login = self.client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "change-me-123"},
        )
        self.assertEqual(login.status_code, 200)
        login_body = login.json()
        session_headers = {"authorization": f"Bearer {login_body['access_token']}"}

        session_tokens = asyncio.run(
            self.context.api_token_repo.list_by_owner(login_body["user"]["id"])
        )
        self.assertEqual(len(session_tokens), 1)
        stale_scopes = [
            scope for scope in session_tokens[0].scopes if not scope.startswith("conversations:")
        ]
        asyncio.run(
            self.context.api_token_repo.update(session_tokens[0].id, {"scopes": stale_scopes})
        )

        conversations = self.client.get("/conversations", headers=session_headers)

        self.assertEqual(conversations.status_code, 200)
        refreshed_token = asyncio.run(self.context.api_token_repo.get(session_tokens[0].id))
        self.assertIsNotNone(refreshed_token)
        assert refreshed_token is not None
        self.assertIn("conversations:read", refreshed_token.scopes)
        self.assertIn("conversations:write", refreshed_token.scopes)

    def test_credential_update_requires_the_current_password(self) -> None:
        self.client.post(
            "/auth/bootstrap",
            json={"email": "admin@example.com", "password": "change-me-123"},
        )
        login = self.client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "change-me-123"},
        )
        session_headers = {"authorization": f"Bearer {login.json()['access_token']}"}

        update = self.client.patch(
            "/auth/me/credentials",
            headers=session_headers,
            json={
                "email": "owner@example.com",
                "current_password": "wrong-password",
            },
        )

        self.assertEqual(update.status_code, 401)
        self.assertEqual(update.json()["detail"], "Current password is incorrect.")
        self.assertEqual(self.client.get("/auth/me", headers=session_headers).status_code, 200)

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

        email_update = self.client.patch(
            "/auth/me/credentials",
            headers={"authorization": f"Bearer {login.json()['access_token']}"},
            json={
                "email": "owner@example.com",
                "current_password": "change-me",
            },
        )
        self.assertEqual(email_update.status_code, 200)

        stale_sync = asyncio.run(
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
        self.assertEqual(stale_sync.email, "owner@example.com")
        self.assertEqual(stale_sync.provider_account_id, "owner@example.com")

        login_with_preserved_password = self.client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": "change-me"},
        )
        self.assertEqual(login_with_preserved_password.status_code, 200)

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

    def test_login_rate_limits_repeated_failures(self) -> None:
        self.client.post(
            "/auth/bootstrap",
            json={"email": "rate-limit@example.com", "password": "change-me-123"},
        )
        for _ in range(5):
            response = self.client.post(
                "/auth/login",
                json={"email": "rate-limit@example.com", "password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 401)

        limited = self.client.post(
            "/auth/login",
            json={"email": "rate-limit@example.com", "password": "wrong-password"},
        )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "60")

    def test_login_rate_limit_reservation_is_atomic_across_concurrent_attempts(self) -> None:
        email = "concurrent-rate-limit@example.com"
        self.client.post(
            "/auth/bootstrap",
            json={"email": email, "password": "change-me-123"},
        )
        _LOCAL_AUTH_FAILURES.clear()

        async def attempt(service: LocalAuthService) -> str:
            try:
                result = await service.authenticate(email=email, password="wrong-password")
            except LocalAuthRateLimitError:
                return "limited"
            self.assertIsNone(result)
            return "failed"

        async def run_attempts() -> list[str]:
            services = [LocalAuthService(self.context) for _ in range(12)]
            return await asyncio.gather(*(attempt(service) for service in services))

        try:
            with patch("app.services.local_auth._verify_password", return_value=False) as verifier:
                results = asyncio.run(run_attempts())
            self.assertEqual(results.count("failed"), 5)
            self.assertEqual(results.count("limited"), 7)
            self.assertEqual(verifier.call_count, 5)
        finally:
            _LOCAL_AUTH_FAILURES.clear()

    def test_login_rate_limit_reservation_is_atomic_across_threads(self) -> None:
        email = "threaded-rate-limit@example.com"
        _LOCAL_AUTH_FAILURES.clear()

        def reserve() -> str:
            try:
                _reserve_local_auth_attempt(email, 1000.0)
            except LocalAuthRateLimitError:
                return "limited"
            return "reserved"

        try:
            with ThreadPoolExecutor(max_workers=12) as executor:
                results = list(executor.map(lambda _: reserve(), range(12)))
            self.assertEqual(results.count("reserved"), 5)
            self.assertEqual(results.count("limited"), 7)
        finally:
            _LOCAL_AUTH_FAILURES.clear()

    def test_login_rate_limit_bounds_keys_and_prunes_expired_entries(self) -> None:
        _LOCAL_AUTH_FAILURES.clear()
        clock = [1000.0]
        service = LocalAuthService(self.context)

        async def fail(email: str):
            return await service.authenticate(email=email, password="wrong-password")

        try:
            with patch("app.services.local_auth.LOCAL_AUTH_MAX_TRACKED_FAILURE_KEYS", 2), patch(
                    "app.services.local_auth.time.monotonic",
                    side_effect=lambda: clock[0],
            ):
                self.assertIsNone(asyncio.run(fail("random-one@example.com")))
                self.assertIsNone(asyncio.run(fail("random-two@example.com")))
                with self.assertRaises(LocalAuthRateLimitError):
                    asyncio.run(fail("random-three@example.com"))

                clock[0] += 60.0
                self.assertIsNone(asyncio.run(fail("random-three@example.com")))
                self.assertEqual(set(_LOCAL_AUTH_FAILURES), {"random-three@example.com"})
        finally:
            _LOCAL_AUTH_FAILURES.clear()

    def test_concurrent_bootstrap_creates_only_one_admin(self) -> None:
        service = LocalAuthService(self.context)

        async def bootstrap(email: str):
            try:
                return await service.bootstrap_local_admin(email=email, password="change-me-123")
            except LocalAuthBootstrapUnavailableError:
                return None

        async def run_both():
            return await asyncio.gather(
                bootstrap("first@example.com"),
                bootstrap("second@example.com"),
            )

        first, second = asyncio.run(run_both())
        self.assertEqual(sum(result is not None for result in (first, second)), 1)

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
