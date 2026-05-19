from __future__ import annotations

import unittest

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.api.routes import create_api_router
from tests.architecture.checks import (
    find_direct_crewai_import_violations,
    find_legacy_import_violations,
    find_legacy_tool_import_violations,
)


class ArchitectureValidationTests(unittest.TestCase):
    def test_all_expected_routers_are_registered(self) -> None:
        app = create_app(context=create_test_api_context())
        paths = {getattr(route, "path", "") for route in app.routes}
        expected = {
            "/agents",
            "/tools",
            "/model-providers",
            "/model-profiles",
            "/mcp-servers",
            "/runtime-adapters",
            "/schedules",
            "/workflows",
            "/executions",
            "/health",
            "/.well-known/agent-card.json",
        }
        self.assertTrue(expected.issubset(paths))

    def test_api_router_factory_includes_all_major_groups(self) -> None:
        router = create_api_router(create_test_api_context())
        prefixes = {route.path for route in router.routes}
        self.assertIn("/agents", prefixes)
        self.assertIn("/executions", prefixes)
        self.assertIn("/tools", prefixes)
        self.assertIn("/schedules", prefixes)

    def test_no_direct_crewai_imports_outside_runtime_adapter_boundary(self) -> None:
        self.assertEqual(find_direct_crewai_import_violations(), [])

    def test_no_legacy_imports_in_repo_owned_python_modules(self) -> None:
        self.assertEqual(find_legacy_import_violations(), [])

    def test_no_legacy_tool_imports_from_app_owned_modules(self) -> None:
        self.assertEqual(find_legacy_tool_import_violations(), [])


if __name__ == "__main__":
    unittest.main()
