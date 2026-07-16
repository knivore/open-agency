from __future__ import annotations

import unittest
from datetime import timedelta
from fastapi.testclient import TestClient
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.time import utc_now
from app.domain import ConnectorInstallation, CredentialDefinition, OneCLIIdentityMapping
from app.services.runtime_secrets import seal_runtime_secret


class _HttpxResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class ConnectorHealthApiTests(unittest.TestCase):
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

    def _run(self, awaitable):
        import asyncio

        return asyncio.run(awaitable)

    def test_telegram_connector_health_uses_env_secret(self) -> None:
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-telegram",
                    owner_user_id="user-1",
                    name="Telegram Bot",
                    provider="telegram",
                    secret_ref="env://TELEGRAM_BOT_TOKEN",
                    metadata={},
                )
            )
        )

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "telegram-token"}, clear=False), patch(
                "app.services.connectors.httpx.request",
                return_value=_HttpxResponse(200, {"ok": True, "result": {"id": 123, "username": "agency_bot"}}),
        ) as mock_request:
            response = self.client.get("/integrations/connectors/credential-telegram/health",
                                       headers=self.owner_headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["audit_execution_id"].startswith("connector-test-"))
        self.assertEqual(payload["provider"], "telegram-bot")
        self.assertEqual(payload["secret_source"], "env")
        mock_request.assert_called_once()

        execution_events = self._run(self.context.execution_store.list_events(payload["audit_execution_id"]))
        event_types = [event.event_type.value for event in execution_events]
        self.assertIn("tool.call.started", event_types)
        self.assertIn("tool.call.completed", event_types)
        audit_workflow = self._run(self.context.workflow_repo.get("connector-test"))
        self.assertIsNotNone(audit_workflow)
        self.assertEqual(audit_workflow.metadata["mode"], "connector_health_test")

    def test_telegram_connector_health_resolves_runtime_secret_mirror(self) -> None:
        installation_id = "connector-installation-telegram-runtime"
        self._run(
            self.context.connector_installation_repo.create(
                ConnectorInstallation(
                    id=installation_id,
                    owner_user_id="user-1",
                    provider="telegram",
                    name="Telegram Runtime",
                    onecli_credential_ref=f"secret://agency/installations/{installation_id}",
                    runtime_secret_encrypted=seal_runtime_secret("telegram-token"),
                    status="active",
                    metadata={},
                )
            )
        )
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-telegram-runtime",
                    owner_user_id="user-1",
                    name="Telegram Runtime",
                    provider="telegram",
                    secret_ref=f"secret://agency/installations/{installation_id}",
                    metadata={},
                )
            )
        )

        with patch("app.services.connectors.httpx.request",
                   return_value=_HttpxResponse(200, {"ok": True, "result": {"id": 123, "username": "agency_bot"}}),
        ) as mock_request:
            response = self.client.get(
                "/integrations/connectors/credential-telegram-runtime/health",
                headers=self.owner_headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "telegram-bot")
        self.assertEqual(payload["secret_source"], "agency")
        self.assertEqual(payload["secret_identifier"], installation_id)
        mock_request.assert_called_once()
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["url"], "https://api.telegram.org/bottelegram-token/getMe")
        self.assertIsNone(kwargs["headers"])

    def test_telegram_connector_health_uses_explicit_ca_bundle(self) -> None:
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-telegram-ca",
                    owner_user_id="user-1",
                    name="Telegram CA",
                    provider="telegram",
                    secret_ref="env://TELEGRAM_BOT_TOKEN",
                    metadata={},
                )
            )
        )

        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": "telegram-token", "SSL_CERT_FILE": "/tmp/agency-ca.pem"},
            clear=False,
        ), patch(
            "app.core.tls.macos_direct_ca_bundle",
            return_value="/tmp/agency-direct-ca-merged.pem",
        ), patch(
            "app.services.connectors.httpx.request",
            return_value=_HttpxResponse(200, {"ok": True, "result": {"id": 123, "username": "agency_bot"}}),
        ) as mock_request:
            response = self.client.get(
                "/integrations/connectors/credential-telegram-ca/health",
                headers=self.owner_headers,
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["verify"], "/tmp/agency-direct-ca-merged.pem")
        self.assertEqual(kwargs["trust_env"], False)

    def test_whatsapp_connector_health_requires_phone_number_id(self) -> None:
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-whatsapp",
                    owner_user_id="user-1",
                    name="WhatsApp Cloud",
                    provider="whatsapp",
                    secret_ref="env://WHATSAPP_TOKEN",
                    metadata={"phone_number_id": "1234567890"},
                )
            )
        )

        with patch.dict("os.environ", {"WHATSAPP_TOKEN": "wa-token"}, clear=False), patch(
                "app.services.connectors.httpx.request",
                return_value=_HttpxResponse(200, {"id": "1234567890", "display_phone_number": "+15551234567"}),
        ) as mock_request:
            response = self.client.post("/integrations/connectors/credential-whatsapp/test", headers=self.owner_headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["audit_execution_id"].startswith("connector-test-"))
        self.assertEqual(payload["provider"], "whatsapp-cloud-api")
        mock_request.assert_called_once()

    def test_discord_connector_health_returns_unresolved_secret_error_for_non_env_refs(self) -> None:
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-discord",
                    owner_user_id="user-1",
                    name="Discord Bot",
                    provider="discord",
                    secret_ref="secret://agency/discord-token",
                    metadata={},
                )
            )
        )

        response = self.client.get("/integrations/connectors/credential-discord/health", headers=self.owner_headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["audit_execution_id"].startswith("connector-test-"))
        self.assertEqual(payload["provider"], "discord-bot")
        self.assertEqual(payload["secret_source"], "unresolved")
        self.assertIn("env://VAR_NAME", payload["error"])

        execution_events = self._run(self.context.execution_store.list_events(payload["audit_execution_id"]))
        event_types = [event.event_type.value for event in execution_events]
        self.assertIn("tool.call.started", event_types)
        self.assertIn("tool.call.failed", event_types)

    def test_discord_connector_health_can_route_through_onecli_without_raw_token(self) -> None:
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-discord-onecli",
                    owner_user_id="user-1",
                    name="Discord Bot OneCLI",
                    provider="discord",
                    secret_ref="onecli://discord/dev-bot",
                    metadata={},
                )
            )
        )

        with patch.dict(
                "os.environ",
                {
                    "ONECLI_ENABLED": "true",
                    "ONECLI_GATEWAY_URL": "http://onecli:10255",
                    "ONECLI_AGENT_TOKEN_SECRET_REF": "env://ONECLI_AGENT_TOKEN",
                    "ONECLI_AGENT_TOKEN": "test-onecli-agent-token",
                },
                clear=False,
        ), patch(
                "app.services.connectors.httpx.request",
                return_value=_HttpxResponse(200, {"id": "discord-bot-id"}),
        ) as mock_request:
            from app.core.config import reset_settings_cache

            reset_settings_cache()
            response = self.client.get(
                "/integrations/connectors/credential-discord-onecli/health",
                headers=self.owner_headers,
            )
            reset_settings_cache()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "discord-bot")
        self.assertEqual(payload["credential_mode"], "onecli")
        self.assertEqual(payload["secret_source"], "onecli")
        self.assertEqual(payload["secret_identifier"], "discord/dev-bot")
        self.assertEqual(payload["onecli"]["gateway_url"], "http://onecli:10255")
        self.assertTrue(payload["onecli"]["agent_token_secret_ref_configured"])
        self.assertNotIn("env://ONECLI_AGENT_TOKEN", str(payload))

        mock_request.assert_called_once()
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["url"], "https://discord.com/api/v10/users/@me")
        self.assertIsNone(kwargs["headers"])
        self.assertEqual(kwargs["proxy"], "http://x:test-onecli-agent-token@onecli:10255")

    def test_discord_connector_health_prefers_owner_onecli_mapping(self) -> None:
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-discord-onecli-owner",
                    owner_user_id="user-1",
                    name="Discord Bot OneCLI Owner",
                    provider="discord",
                    secret_ref="onecli://users/user-1/discord-bot/credential-discord-onecli-owner",
                    metadata={},
                )
            )
        )
        self._run(
            self.context.onecli_identity_mapping_repo.create(
                OneCLIIdentityMapping(
                    id="mapping-user-1-onecli",
                    owner_user_id="user-1",
                    name="User One OneCLI",
                    onecli_agent_id="onecli-agent-user-1",
                    agent_token_secret_ref="env://ONECLI_OWNER_AGENT_TOKEN",
                )
            )
        )

        with patch.dict(
                "os.environ",
                {
                    "ONECLI_ENABLED": "true",
                    "ONECLI_GATEWAY_URL": "http://onecli:10255",
                    "ONECLI_AGENT_TOKEN_SECRET_REF": "env://ONECLI_GLOBAL_AGENT_TOKEN",
                    "ONECLI_GLOBAL_AGENT_TOKEN": "global-onecli-agent-token",
                    "ONECLI_OWNER_AGENT_TOKEN": "mapped-onecli-agent-token",
                },
                clear=False,
        ), patch(
                "app.services.connectors.httpx.request",
                return_value=_HttpxResponse(200, {"id": "discord-bot-id"}),
        ) as mock_request:
            from app.core.config import reset_settings_cache

            reset_settings_cache()
            response = self.client.get(
                "/integrations/connectors/credential-discord-onecli-owner/health",
                headers=self.owner_headers,
            )
            reset_settings_cache()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["onecli"]["agent_token_secret_ref_configured"])
        self.assertEqual(payload["onecli"]["agent_identity"]["mapping"], "user_mapping")
        self.assertEqual(payload["onecli"]["agent_identity"]["mapping_id"], "mapping-user-1-onecli")
        self.assertEqual(payload["onecli"]["agent_identity"]["onecli_agent_id"], "onecli-agent-user-1")
        self.assertNotIn("env://ONECLI_OWNER_AGENT_TOKEN", str(payload))

        mock_request.assert_called_once()
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["proxy"], "http://x:mapped-onecli-agent-token@onecli:10255")

    def test_telegram_connector_health_uses_onecli_url_path_injection(self) -> None:
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-telegram-onecli",
                    owner_user_id="user-1",
                    name="Telegram Bot OneCLI",
                    provider="telegram",
                    secret_ref="onecli://telegram/dev-bot",
                    metadata={},
                )
            )
        )

        with patch.dict("os.environ", {"ONECLI_ENABLED": "true"}, clear=False), patch(
                "app.services.connectors.httpx.request",
                return_value=_HttpxResponse(
                    200,
                    {"ok": True, "result": {"id": 123, "username": "agency_bot"}},
                ),
        ) as mock_request:
            from app.core.config import reset_settings_cache

            reset_settings_cache()
            response = self.client.get(
                "/integrations/connectors/credential-telegram-onecli/health",
                headers=self.owner_headers,
            )
            reset_settings_cache()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "telegram-bot")
        self.assertEqual(payload["credential_mode"], "onecli")
        self.assertEqual(payload["secret_source"], "onecli")
        mock_request.assert_called_once()
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["url"], "https://api.telegram.org/botonecli-managed/getMe")
        self.assertIsNone(kwargs["headers"])
        self.assertIsNotNone(kwargs["proxy"])

    def test_whatsapp_connector_health_can_route_through_onecli_without_raw_token(self) -> None:
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-whatsapp-onecli",
                    owner_user_id="user-1",
                    name="WhatsApp OneCLI",
                    provider="whatsapp",
                    secret_ref="onecli://whatsapp/dev-phone",
                    metadata={"phone_number_id": "1234567890"},
                )
            )
        )

        with patch.dict(
                "os.environ",
                {
                    "ONECLI_ENABLED": "true",
                    "ONECLI_GATEWAY_URL": "http://onecli:10255",
                    "ONECLI_AGENT_TOKEN_SECRET_REF": "",
                    "ONECLI_AGENT_TOKEN": "",
                    "ONECLI_ALLOW_GLOBAL_AGENT_TOKEN_FALLBACK": "false",
                },
                clear=False,
        ), patch(
                "app.services.connectors.httpx.request",
                return_value=_HttpxResponse(200, {"id": "1234567890", "display_phone_number": "+15551234567"}),
        ) as mock_request:
            from app.core.config import reset_settings_cache

            reset_settings_cache()
            response = self.client.get(
                "/integrations/connectors/credential-whatsapp-onecli/health",
                headers=self.owner_headers,
            )
            reset_settings_cache()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "whatsapp-cloud-api")
        self.assertEqual(payload["credential_mode"], "onecli")
        self.assertEqual(payload["secret_source"], "onecli")
        self.assertEqual(payload["secret_identifier"], "whatsapp/dev-phone")

        mock_request.assert_called_once()
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["url"], "https://graph.facebook.com/v20.0/1234567890")
        self.assertIsNone(kwargs["headers"])
        self.assertEqual(kwargs["proxy"], "http://onecli:10255")

    def test_connector_health_hides_cross_owner_credentials(self) -> None:
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-owner-only",
                    owner_user_id="user-1",
                    name="Owner Only Connector",
                    provider="telegram",
                    secret_ref="env://TELEGRAM_BOT_TOKEN",
                    metadata={},
                )
            )
        )

        response = self.client.get(
            "/integrations/connectors/credential-owner-only/health",
            headers=self.other_owner_headers,
        )

        self.assertEqual(response.status_code, 404)

    def test_connector_health_history_lists_audited_runs_for_owner_only(self) -> None:
        before_window = utc_now()
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-history",
                    owner_user_id="user-1",
                    name="History Connector",
                    provider="telegram",
                    secret_ref="env://TELEGRAM_BOT_TOKEN",
                    metadata={},
                )
            )
        )

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "telegram-token"}, clear=False), patch(
                "app.services.connectors.httpx.request",
                return_value=_HttpxResponse(200, {"ok": True, "result": {"id": 123, "username": "agency_bot"}}),
        ):
            test_response = self.client.get("/integrations/connectors/credential-history/health",
                                            headers=self.owner_headers)

        self.assertEqual(test_response.status_code, 200)

        with patch.dict("os.environ", {}, clear=False):
            failed_test_response = self.client.get(
                "/integrations/connectors/credential-history/health",
                headers=self.owner_headers,
            )
        self.assertEqual(failed_test_response.status_code, 200)
        after_window = utc_now()

        history_response = self.client.get(
            "/integrations/connectors/credential-history/history",
            headers=self.owner_headers,
        )
        self.assertEqual(history_response.status_code, 200)
        payload = history_response.json()
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["limit"], 20)
        self.assertEqual(payload["offset"], 0)
        self.assertIsNone(payload["status"])
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(payload["items"][0]["executionId"], failed_test_response.json()["audit_execution_id"])
        self.assertEqual(payload["items"][1]["executionId"], test_response.json()["audit_execution_id"])
        self.assertEqual(payload["items"][0]["provider"], "telegram")
        self.assertIn("tool.call.completed", payload["items"][1]["eventTypes"])

        filtered_response = self.client.get(
            "/integrations/connectors/credential-history/history",
            headers=self.owner_headers,
            params={"status": "completed", "limit": 1, "offset": 0},
        )
        self.assertEqual(filtered_response.status_code, 200)
        filtered_payload = filtered_response.json()
        self.assertEqual(filtered_payload["total"], 1)
        self.assertEqual(filtered_payload["limit"], 1)
        self.assertEqual(filtered_payload["offset"], 0)
        self.assertEqual(filtered_payload["status"], "completed")
        self.assertEqual(len(filtered_payload["items"]), 1)
        self.assertEqual(filtered_payload["items"][0]["executionId"], test_response.json()["audit_execution_id"])

        time_filtered_response = self.client.get(
            "/integrations/connectors/credential-history/history",
            headers=self.owner_headers,
            params={
                "started_after": before_window.isoformat(),
                "started_before": after_window.isoformat(),
            },
        )
        self.assertEqual(time_filtered_response.status_code, 200)
        time_filtered_payload = time_filtered_response.json()
        self.assertEqual(time_filtered_payload["total"], 2)
        self.assertIsNotNone(time_filtered_payload["startedAfter"])
        self.assertIsNotNone(time_filtered_payload["startedBefore"])

        future_filtered_response = self.client.get(
            "/integrations/connectors/credential-history/history",
            headers=self.owner_headers,
            params={
                "started_after": (after_window + timedelta(seconds=1)).isoformat(),
            },
        )
        self.assertEqual(future_filtered_response.status_code, 200)
        future_filtered_payload = future_filtered_response.json()
        self.assertEqual(future_filtered_payload["total"], 0)
        self.assertEqual(len(future_filtered_payload["items"]), 0)

        cross_owner_response = self.client.get(
            "/integrations/connectors/credential-history/history",
            headers=self.other_owner_headers,
        )
        self.assertEqual(cross_owner_response.status_code, 404)

    def test_connector_health_history_across_all_credentials_supports_provider_and_status_filters(self) -> None:
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-aggregate-telegram",
                    owner_user_id="user-1",
                    name="Aggregate Telegram",
                    provider="telegram",
                    secret_ref="env://TELEGRAM_BOT_TOKEN",
                    metadata={},
                )
            )
        )
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-aggregate-discord",
                    owner_user_id="user-1",
                    name="Aggregate Discord",
                    provider="discord",
                    secret_ref="secret://agency/discord-token",
                    metadata={},
                )
            )
        )

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "telegram-token"}, clear=False), patch(
                "app.services.connectors.httpx.request",
                return_value=_HttpxResponse(200, {"ok": True, "result": {"id": 123, "username": "agency_bot"}}),
        ):
            telegram_response = self.client.get(
                "/integrations/connectors/credential-aggregate-telegram/health",
                headers=self.owner_headers,
            )
        self.assertEqual(telegram_response.status_code, 200)

        discord_response = self.client.get(
            "/integrations/connectors/credential-aggregate-discord/health",
            headers=self.owner_headers,
        )
        self.assertEqual(discord_response.status_code, 200)

        aggregate_response = self.client.get(
            "/integrations/connectors/history",
            headers=self.owner_headers,
        )
        self.assertEqual(aggregate_response.status_code, 200)
        aggregate_payload = aggregate_response.json()
        self.assertEqual(aggregate_payload["total"], 2)
        credential_ids = [item["credentialId"] for item in aggregate_payload["items"]]
        self.assertIn("credential-aggregate-telegram", credential_ids)
        self.assertIn("credential-aggregate-discord", credential_ids)

        provider_filtered_response = self.client.get(
            "/integrations/connectors/history",
            headers=self.owner_headers,
            params={"provider": "telegram"},
        )
        self.assertEqual(provider_filtered_response.status_code, 200)
        provider_filtered_payload = provider_filtered_response.json()
        self.assertEqual(provider_filtered_payload["total"], 1)
        self.assertEqual(provider_filtered_payload["items"][0]["credentialId"], "credential-aggregate-telegram")

        status_filtered_response = self.client.get(
            "/integrations/connectors/history",
            headers=self.owner_headers,
            params={"status": "failed"},
        )
        self.assertEqual(status_filtered_response.status_code, 200)
        status_filtered_payload = status_filtered_response.json()
        self.assertEqual(status_filtered_payload["total"], 1)
        self.assertEqual(status_filtered_payload["items"][0]["credentialId"], "credential-aggregate-discord")

    def test_connector_health_history_prune_supports_keep_latest_per_credential(self) -> None:
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-prune",
                    owner_user_id="user-1",
                    name="Prune Connector",
                    provider="telegram",
                    secret_ref="env://TELEGRAM_BOT_TOKEN",
                    metadata={},
                )
            )
        )

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "telegram-token"}, clear=False), patch(
                "app.services.connectors.httpx.request",
                return_value=_HttpxResponse(200, {"ok": True, "result": {"id": 123, "username": "agency_bot"}}),
        ):
            first = self.client.get("/integrations/connectors/credential-prune/health", headers=self.owner_headers)
            second = self.client.get("/integrations/connectors/credential-prune/health", headers=self.owner_headers)
            third = self.client.get("/integrations/connectors/credential-prune/health", headers=self.owner_headers)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(third.status_code, 200)

        prune_response = self.client.delete(
            "/integrations/connectors/credential-prune/history",
            headers=self.owner_headers,
            params={"keep_latest": 1},
        )
        self.assertEqual(prune_response.status_code, 200)
        prune_payload = prune_response.json()
        self.assertEqual(prune_payload["matched"], 3)
        self.assertEqual(prune_payload["deleted"], 2)
        self.assertEqual(prune_payload["retained"], 1)
        self.assertEqual(prune_payload["credentialId"], "credential-prune")
        self.assertEqual(prune_payload["keepLatest"], 1)

        remaining_history = self.client.get(
            "/integrations/connectors/credential-prune/history",
            headers=self.owner_headers,
        )
        self.assertEqual(remaining_history.status_code, 200)
        remaining_payload = remaining_history.json()
        self.assertEqual(remaining_payload["total"], 1)
        self.assertEqual(remaining_payload["items"][0]["executionId"], third.json()["audit_execution_id"])

        self.assertIsNone(self._run(self.context.execution_store.get_execution(first.json()["audit_execution_id"])))
        self.assertEqual(self._run(self.context.execution_store.list_events(first.json()["audit_execution_id"])), [])

    def test_connector_health_history_prune_supports_aggregate_filters(self) -> None:
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-prune-telegram",
                    owner_user_id="user-1",
                    name="Prune Telegram",
                    provider="telegram",
                    secret_ref="env://TELEGRAM_BOT_TOKEN",
                    metadata={},
                )
            )
        )
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-prune-discord",
                    owner_user_id="user-1",
                    name="Prune Discord",
                    provider="discord",
                    secret_ref="secret://agency/discord-token",
                    metadata={},
                )
            )
        )

        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "telegram-token"}, clear=False), patch(
                "app.services.connectors.httpx.request",
                return_value=_HttpxResponse(200, {"ok": True, "result": {"id": 123, "username": "agency_bot"}}),
        ):
            self.client.get("/integrations/connectors/credential-prune-telegram/health", headers=self.owner_headers)
        self.client.get("/integrations/connectors/credential-prune-discord/health", headers=self.owner_headers)

        prune_response = self.client.delete(
            "/integrations/connectors/history",
            headers=self.owner_headers,
            params={"status": "failed", "provider": "discord"},
        )
        self.assertEqual(prune_response.status_code, 200)
        prune_payload = prune_response.json()
        self.assertEqual(prune_payload["matched"], 1)
        self.assertEqual(prune_payload["deleted"], 1)
        self.assertEqual(prune_payload["retained"], 0)
        self.assertEqual(prune_payload["status"], "failed")
        self.assertEqual(prune_payload["provider"], "discord")

        aggregate_response = self.client.get(
            "/integrations/connectors/history",
            headers=self.owner_headers,
        )
        self.assertEqual(aggregate_response.status_code, 200)
        aggregate_payload = aggregate_response.json()
        self.assertEqual(aggregate_payload["total"], 1)
        self.assertEqual(aggregate_payload["items"][0]["credentialId"], "credential-prune-telegram")


if __name__ == "__main__":
    unittest.main()
