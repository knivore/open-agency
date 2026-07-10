from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.tools.discovery import (
    discover_allowed_python_tool_modules,
    discover_builtin_tool_modules,
    discover_generated_tool_modules,
    discover_integration_tool_modules,
    discover_integrations,
)
from app.tools.registry import ToolRegistry


class IntegrationDiscoveryTests(unittest.TestCase):
    def test_repo_sample_integration_is_discovered(self) -> None:
        discovered = discover_integrations(strict=True)

        self.assertTrue(any(item.manifest.id == "sample-integration" for item in discovered))
        modules = discover_integration_tool_modules(strict=True)
        self.assertIn("integrations.sample_integration.tools", modules)

    def test_repo_sample_generated_tool_is_discovered(self) -> None:
        modules = discover_generated_tool_modules(strict=True)
        self.assertIn("generated_tools.sample_generated_tool.tools", modules)

    def test_invalid_manifest_is_skipped_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            broken = root / "broken"
            broken.mkdir()
            (broken / "manifest.yaml").write_text(
                "\n".join(
                    [
                        "id: broken",
                        "name: Broken",
                        "module_root: integrations.broken",
                        "tool_modules:",
                        "  - integrations.broken.tools",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(discover_integrations(root=root), [])

    def test_invalid_manifest_raises_in_strict_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            broken = root / "broken"
            broken.mkdir()
            (broken / "manifest.yaml").write_text(
                "\n".join(
                    [
                        "id: broken",
                        "name: Broken",
                        "module_root: integrations.broken",
                        "tool_modules:",
                        "  - integrations.broken.tools",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(RuntimeError):
                discover_integrations(root=root, strict=True)

    def test_tool_registry_allowlist_includes_integration_modules(self) -> None:
        registry = ToolRegistry()

        self.assertIn("integrations.sample_integration.tools", registry.default_python_allowlist)
        self.assertIn("generated_tools.sample_generated_tool.tools", registry.default_python_allowlist)
        self.assertTrue(set(discover_builtin_tool_modules()).issubset(registry.default_python_allowlist))
        self.assertTrue(set(discover_allowed_python_tool_modules()).issubset(registry.default_python_allowlist))
