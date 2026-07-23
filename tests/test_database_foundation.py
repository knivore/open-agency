from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import get_settings, reset_settings_cache
from app.db.models import Base
from app.db.session import ping_database, reset_session_state


class DatabaseFoundationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_settings_cache()
        reset_session_state()

    def tearDown(self) -> None:
        reset_settings_cache()
        reset_session_state()

    def test_config_loads_database_url(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "test",
                    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/agency",
                    "DATABASE_ECHO": "true",
                    "DATABASE_POOL_SIZE": "9",
                    "DATABASE_MAX_OVERFLOW": "4",
                },
                clear=False,
        ):
            reset_settings_cache()
            settings = get_settings()

        self.assertEqual(settings.database_url, "postgresql://postgres:postgres@localhost:5432/agency")
        self.assertEqual(settings.sqlalchemy_database_url,
                         "postgresql+asyncpg://postgres:postgres@localhost:5432/agency")
        self.assertTrue(settings.database_echo)
        self.assertEqual(settings.database_pool_size, 9)
        self.assertEqual(settings.database_max_overflow, 4)

    def test_config_loads_physical_policy_limits(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "AGENCY_PHYSICAL_POLICY_REQUIRE_CONFIRMATION_FOR_RISKY_COMMANDS": "false",
                    "AGENCY_PHYSICAL_POLICY_MIN_CLIMATE_TEMPERATURE_CELSIUS": "16",
                    "AGENCY_PHYSICAL_POLICY_MAX_CLIMATE_TEMPERATURE_CELSIUS": "26",
                    "AGENCY_PHYSICAL_POLICY_RESTRICTED_ROOMS": "nursery,garage",
                    "AGENCY_PHYSICAL_POLICY_BLOCKED_USERS": "agent:blocked",
                    "AGENCY_PHYSICAL_POLICY_PRESENCE_REQUIRED_ROOMS": "entry",
                    "AGENCY_PHYSICAL_POLICY_QUIET_HOURS": "22:00-07:00",
                },
                clear=False,
        ):
            reset_settings_cache()
            settings = get_settings()

        self.assertFalse(settings.agency_physical_policy_require_confirmation_for_risky_commands)
        self.assertEqual(settings.agency_physical_policy_min_climate_temperature_celsius, 16)
        self.assertEqual(settings.agency_physical_policy_max_climate_temperature_celsius, 26)
        self.assertEqual(settings.agency_physical_policy_restricted_rooms, "nursery,garage")
        self.assertEqual(settings.agency_physical_policy_blocked_users, "agent:blocked")
        self.assertEqual(settings.agency_physical_policy_presence_required_rooms, "entry")
        self.assertEqual(settings.agency_physical_policy_quiet_hours, "22:00-07:00")

    def test_config_rejects_invalid_physical_policy_temperature_range(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "AGENCY_PHYSICAL_POLICY_MIN_CLIMATE_TEMPERATURE_CELSIUS": "30",
                    "AGENCY_PHYSICAL_POLICY_MAX_CLIMATE_TEMPERATURE_CELSIUS": "20",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaises(RuntimeError):
                get_settings().ensure_runtime_requirements()

    def test_compose_postgres_increases_shared_lock_capacity(self) -> None:
        compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

        self.assertEqual(
            compose["services"]["postgres"]["command"],
            [
                "postgres",
                "-c",
                "max_locks_per_transaction=${POSTGRES_MAX_LOCKS_PER_TRANSACTION:-256}",
            ],
        )

    async def test_db_session_can_connect_with_sqlite_fallback(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "test",
                    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                },
                clear=False,
        ):
            reset_settings_cache()
            reset_session_state()
            connected = await ping_database()

        self.assertTrue(connected)

    def test_health_db_returns_expected_result(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "test",
                    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                },
                clear=False,
        ):
            reset_settings_cache()
            reset_session_state()
            client = TestClient(create_app(context=create_test_api_context()))
            response = client.get("/health/db")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "configured": True})

    def test_alembic_metadata_imports_successfully(self) -> None:
        env_path = Path("/Users/kehchinleong/Documents/Personal/Agency/agency/alembic/env.py")
        spec = importlib.util.spec_from_file_location("test_alembic_env", env_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        self.assertIs(module.target_metadata, Base.metadata)
        self.assertIn("agents", module.target_metadata.tables)
        self.assertIn("api_tokens", module.target_metadata.tables)
        self.assertIn("conversations", module.target_metadata.tables)
        self.assertIn("conversation_approval_requests", module.target_metadata.tables)
        self.assertIn("main_agent_profiles", module.target_metadata.tables)
        self.assertIn("users", module.target_metadata.tables)
        self.assertIn("onecli_identity_mappings", module.target_metadata.tables)
        self.assertIn("public_endpoints", module.target_metadata.tables)
        self.assertIn("executions", module.target_metadata.tables)
        self.assertIn("schedule_fire_claims", module.target_metadata.tables)

    def test_production_mode_requires_database_url(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "production",
                    "DATABASE_URL": "",
                },
                clear=True,
        ):
            reset_settings_cache()
            with self.assertRaises(RuntimeError):
                get_settings().ensure_runtime_requirements()

    def test_production_mode_requires_internal_api_key(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "production",
                    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                    "AGENCY_INTERNAL_API_KEY": "",
                },
                clear=True,
        ):
            reset_settings_cache()
            with self.assertRaises(RuntimeError):
                get_settings().ensure_runtime_requirements()

    def test_isolation_requires_integrations_runtime_flag(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "test",
                    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                    "EXECUTION_ISOLATION_ENABLED": "true",
                    "INTEGRATIONS_RUNTIME_ENABLED": "false",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaises(RuntimeError):
                get_settings().ensure_runtime_requirements()

    def test_isolated_workers_require_container_visible_postgres_url(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "test",
                    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/agency",
                    "EXECUTION_ISOLATION_ENABLED": "true",
                    "INTEGRATIONS_RUNTIME_ENABLED": "true",
                    "EXECUTION_RUNTIME_DATABASE_URL": "",
                    "EXECUTION_CONTAINER_NETWORK": "agency_default",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaisesRegex(RuntimeError, "EXECUTION_RUNTIME_DATABASE_URL"):
                get_settings().ensure_runtime_requirements()

    def test_isolated_workers_accept_container_visible_postgres_url(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "test",
                    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/agency",
                    "EXECUTION_ISOLATION_ENABLED": "true",
                    "INTEGRATIONS_RUNTIME_ENABLED": "true",
                    "EXECUTION_RUNTIME_DATABASE_URL": "postgresql://postgres:postgres@postgres:5432/agency",
                    "EXECUTION_CONTAINER_NETWORK": "agency_default",
                },
                clear=False,
        ):
            reset_settings_cache()
            settings = get_settings()
            settings.ensure_runtime_requirements()

        self.assertEqual(
            settings.container_database_url,
            "postgresql://postgres:postgres@postgres:5432/agency",
        )

    def test_shadow_mode_requires_integrations_runtime_flag(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "test",
                    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                    "RUNTIME_REVISION_SHADOW_MODE": "true",
                    "INTEGRATIONS_RUNTIME_ENABLED": "false",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaises(RuntimeError):
                get_settings().ensure_runtime_requirements()

    def test_production_rejects_host_network_for_isolated_runtime(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "production",
                    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                    "AGENCY_INTERNAL_API_KEY": "test-internal-key",
                    "INTEGRATIONS_RUNTIME_ENABLED": "true",
                    "EXECUTION_ISOLATION_ENABLED": "true",
                    "EXECUTION_CONTAINER_NETWORK": "host",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaises(RuntimeError):
                get_settings().ensure_runtime_requirements()

    def test_production_rejects_replacement_without_reconciler(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "production",
                    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                    "AGENCY_INTERNAL_API_KEY": "test-internal-key",
                    "INTEGRATIONS_RUNTIME_ENABLED": "true",
                    "EXECUTION_ISOLATION_ENABLED": "true",
                    "CANCEL_OUTDATED_EXECUTIONS": "true",
                    "RUNTIME_RECONCILER_ENABLED": "false",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaises(RuntimeError):
                get_settings().ensure_runtime_requirements()

    def test_onecli_enforcement_requires_onecli_enabled(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "test",
                    "ONECLI_ENABLED": "false",
                    "ONECLI_FORCE_FOR_HTTP_TOOLS": "true",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaisesRegex(RuntimeError, "ONECLI_ENABLED"):
                get_settings().ensure_runtime_requirements()

    def test_production_onecli_https_gateway_requires_ca_bundle(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "production",
                    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                    "AGENCY_INTERNAL_API_KEY": "test-internal-key",
                    "ONECLI_ENABLED": "true",
                    "ONECLI_GATEWAY_URL": "https://onecli-gateway.local",
                    "ONECLI_GATEWAY_CA_BUNDLE_PATH": "",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaisesRegex(RuntimeError, "ONECLI_GATEWAY_CA_BUNDLE_PATH"):
                get_settings().ensure_runtime_requirements()

    def test_onecli_worker_enforcement_requires_execution_isolation(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "test",
                    "ONECLI_ENABLED": "true",
                    "ONECLI_FORCE_FOR_ISOLATED_WORKERS": "true",
                    "EXECUTION_ISOLATION_ENABLED": "false",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaisesRegex(RuntimeError, "EXECUTION_ISOLATION_ENABLED"):
                get_settings().ensure_runtime_requirements()

    def test_onecli_worker_internal_egress_requires_worker_enforcement(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "test",
                    "ONECLI_ENABLED": "true",
                    "ONECLI_FORCE_FOR_ISOLATED_WORKERS": "false",
                    "ONECLI_WORKER_EGRESS_MODE": "docker_internal_network",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaisesRegex(RuntimeError, "ONECLI_FORCE_FOR_ISOLATED_WORKERS"):
                get_settings().ensure_runtime_requirements()

    def test_onecli_multi_user_mode_rejects_global_agent_token_fallback(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "test",
                    "ONECLI_ENABLED": "true",
                    "ONECLI_MULTI_USER_MODE": "true",
                    "ONECLI_ALLOW_GLOBAL_AGENT_TOKEN_FALLBACK": "true",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaisesRegex(RuntimeError, "ONECLI_ALLOW_GLOBAL_AGENT_TOKEN_FALLBACK"):
                get_settings().ensure_runtime_requirements()

    def test_production_onecli_worker_enforcement_requires_internal_egress(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "production",
                    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                    "AGENCY_INTERNAL_API_KEY": "test-internal-key",
                    "INTEGRATIONS_RUNTIME_ENABLED": "true",
                    "EXECUTION_ISOLATION_ENABLED": "true",
                    "ONECLI_ENABLED": "true",
                    "ONECLI_FORCE_FOR_ISOLATED_WORKERS": "true",
                    "ONECLI_WORKER_EGRESS_MODE": "proxy_env_only",
                    "OPENAI_API_KEY": "",
                    "ANTHROPIC_API_KEY": "",
                    "GOOGLE_API_KEY": "",
                    "AZURE_API_KEY": "",
                    "AZURE_OPENAI_API_KEY": "",
                    "LOCAL_OPENAI_API_KEY": "",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaisesRegex(RuntimeError, "ONECLI_WORKER_EGRESS_MODE"):
                get_settings().ensure_runtime_requirements()

    def test_production_onecli_enforcement_rejects_direct_external_credentials(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "production",
                    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                    "AGENCY_INTERNAL_API_KEY": "test-internal-key",
                    "ONECLI_ENABLED": "true",
                    "ONECLI_FORCE_FOR_HTTP_TOOLS": "true",
                    "ONECLI_GATEWAY_URL": "http://onecli:10255",
                    "OPENAI_API_KEY": "sk-test",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                get_settings().ensure_runtime_requirements()

    def test_onecli_diagnostics_redact_secret_reference_value(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "ONECLI_ENABLED": "true",
                    "ONECLI_AGENT_TOKEN_SECRET_REF": "env://ONECLI_AGENT_TOKEN",
                },
                clear=False,
        ):
            reset_settings_cache()
            diagnostics = get_settings().sanitized_onecli_diagnostics
            self.assertTrue(diagnostics["agent_token_secret_ref_configured"])
            self.assertNotIn("env://ONECLI_AGENT_TOKEN", diagnostics.values())

    def test_valid_hardened_runtime_configuration_passes(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "APP_ENV": "production",
                    "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
                    "AGENCY_INTERNAL_API_KEY": "test-internal-key",
                    "INTEGRATIONS_RUNTIME_ENABLED": "true",
                    "EXECUTION_ISOLATION_ENABLED": "true",
                    "RUNTIME_RECONCILER_ENABLED": "true",
                    "CANCEL_OUTDATED_EXECUTIONS": "true",
                    "EXECUTION_CONTAINER_NETWORK": "bridge",
                    "EXECUTION_CONTAINER_BIND_INTEGRATIONS_READ_ONLY": "true",
                },
                clear=False,
        ):
            reset_settings_cache()
            get_settings().ensure_runtime_requirements()


if __name__ == "__main__":
    unittest.main()
