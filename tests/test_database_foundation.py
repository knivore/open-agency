from __future__ import annotations

import importlib.util
import os
import unittest
from fastapi.testclient import TestClient
from pathlib import Path
from unittest.mock import patch

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
        self.assertIn("conversations", module.target_metadata.tables)
        self.assertIn("conversation_approval_requests", module.target_metadata.tables)
        self.assertIn("main_agent_profiles", module.target_metadata.tables)
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
