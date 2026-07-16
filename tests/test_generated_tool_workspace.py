from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from app.api.context import create_test_api_context
from app.services.generated_tool_workspace import GeneratedToolWorkspaceService


class GeneratedToolWorkspaceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_scaffolded_tool_stub_returns_explicit_not_implemented_status(self) -> None:
        context = create_test_api_context()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "generated_tools"
            root.mkdir(parents=True, exist_ok=True)
            (root / "__init__.py").write_text('"""temp generated tools root."""\n', encoding="utf-8")
            sys.path.insert(0, tmp_dir)
            try:
                service = GeneratedToolWorkspaceService(context, root_path=root)
                package = service.scaffold_package(
                    package_id="portal-audit",
                    name="Portal Audit",
                    description="Portal inspection helpers.",
                    function_name="audit_portal",
                )
                module_name = f"{package['module_root']}.tools"
                module = service._load_generated_module_from_root(module_name)
                result = module.audit_portal("probe")
            finally:
                sys.path.remove(tmp_dir)

        self.assertEqual(result["status"], "not_implemented")
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "TODO: implement audit_portal")
        self.assertEqual(result["text"], "probe")

    async def test_scaffold_and_publish_generated_tool(self) -> None:
        context = create_test_api_context()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "generated_tools"
            root.mkdir(parents=True, exist_ok=True)
            (root / "__init__.py").write_text('"""temp generated tools root."""\n', encoding="utf-8")
            sys.path.insert(0, tmp_dir)
            try:
                service = GeneratedToolWorkspaceService(context, root_path=root)
                package = service.scaffold_package(
                    package_id="portal-audit",
                    name="Portal Audit",
                    description="Portal inspection helpers.",
                    function_name="audit_portal",
                )
                tool = await service.publish_tool(
                    package_id="portal-audit",
                    tool_id="generated.portal.audit",
                    name="generated_portal_audit",
                    description="Audit the portal.",
                    callable_name="audit_portal",
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    output_schema={
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "additionalProperties": True,
                    },
                    tags=["portal", "audit"],
                )
                packages = await service.list_packages_with_registry()
            finally:
                sys.path.remove(tmp_dir)

        self.assertEqual(package["package_id"], "portal_audit")
        self.assertEqual(tool.implementation.target, "generated_tools.portal_audit.tools")
        self.assertEqual(tool.implementation.callable_name, "audit_portal")
        self.assertIn("generated_tool", tool.tags)
        self.assertEqual(packages["count"], 1)
        self.assertEqual(packages["packages"][0]["package_id"], "portal_audit")
        self.assertEqual(len(packages["packages"][0]["registered_tools"]), 1)
