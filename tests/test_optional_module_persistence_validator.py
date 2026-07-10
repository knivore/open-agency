from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from app.core.config import reset_settings_cache
from scripts.validate_optional_module_persistence import build_report


class OptionalModulePersistenceValidatorTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_settings_cache()

    def test_build_report_allows_core_without_optional_module_persistence_plans(self) -> None:
        report, exit_code = build_report(check_paths=True)

        self.assertEqual(exit_code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["expectedModules"], [])
        self.assertEqual(report["modules"], [])
        self.assertIn("alembic/versions", report["alembicVersionLocations"])

    def test_build_report_accepts_expected_configured_modules(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.external_module_pack.manifest:module_spec"},
            clear=False,
        ):
            reset_settings_cache()
            report, exit_code = build_report(check_paths=True, expected_modules=("external_example_pack",))

        self.assertEqual(exit_code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["expectedModules"], ["external_example_pack"])

    def test_build_report_uses_configured_expected_modules(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.external_module_pack.manifest:module_spec",
                "AGENCY_EXPECTED_OPTIONAL_MODULES": "external_example_pack",
            },
            clear=False,
        ):
            reset_settings_cache()

            report, exit_code = build_report(check_paths=True)

        self.assertEqual(exit_code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["expectedModules"], ["external_example_pack"])

    def test_build_report_flags_missing_expected_module(self) -> None:
        report, exit_code = build_report(check_paths=False, expected_modules=("missing_pack",))

        self.assertEqual(exit_code, 1)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any(
                "expected optional module 'missing_pack' is not registered with a persistence plan" in error
                for error in report["errors"]
            )
        )

    def test_build_report_includes_configured_pack_version_location(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:example_module_spec"},
            clear=False,
        ):
            reset_settings_cache()

            report, exit_code = build_report(check_paths=False)

        self.assertEqual(exit_code, 0)
        modules = {item["module"]: item for item in report["modules"]}
        self.assertIn("example_pack", modules)
        self.assertIn("tests/fixtures/example_pack_migrations", report["alembicVersionLocations"])
        self.assertEqual(modules["example_pack"]["migrationSource"], "package")
        self.assertEqual(modules["example_pack"]["removalPolicy"], "preserve_data")
        self.assertEqual(
            modules["example_pack"]["removalNotes"],
            ["Example pack test data is retained unless an operator drops the table."],
        )
        self.assertIsNone(modules["example_pack"]["migrations"][0]["pathExists"])

    def test_build_report_validates_external_style_pack_paths(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.external_module_pack.manifest:module_spec"},
            clear=False,
        ):
            reset_settings_cache()

            report, exit_code = build_report(check_paths=True, expected_modules=("external_example_pack",))

        self.assertEqual(exit_code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["expectedModules"], ["external_example_pack"])
        modules = {item["module"]: item for item in report["modules"]}
        self.assertIn("external_example_pack", modules)
        self.assertIn("tests/fixtures/external_module_pack/migrations", report["alembicVersionLocations"])
        self.assertEqual(modules["external_example_pack"]["migrationSource"], "package")
        self.assertEqual(modules["external_example_pack"]["removalPolicy"], "preserve_data")
        self.assertTrue(modules["external_example_pack"]["migrations"][0]["pathExists"])
        self.assertTrue(all(ref["importable"] for ref in modules["external_example_pack"]["ormModelRefs"]))

    def test_build_report_flags_duplicate_revision(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:duplicate_revision_module_specs"},
            clear=False,
        ):
            reset_settings_cache()

            report, exit_code = build_report(check_paths=False)

        self.assertEqual(exit_code, 1)
        self.assertFalse(report["ok"])
        self.assertTrue(any("duplicate migration revision 'example_0001'" in error for error in report["errors"]))

    def test_build_report_flags_missing_revision_dependency(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:missing_dependency_module_spec"},
            clear=False,
        ):
            reset_settings_cache()

            report, exit_code = build_report(check_paths=False)

        self.assertEqual(exit_code, 1)
        self.assertTrue(any("depends on unknown optional revision 'does_not_exist_0001'" in error for error in report["errors"]))

    def test_build_report_flags_revision_cycle(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:cyclic_dependency_module_spec"},
            clear=False,
        ):
            reset_settings_cache()

            report, exit_code = build_report(check_paths=False)

        self.assertEqual(exit_code, 1)
        self.assertTrue(any("migration dependency cycle detected" in error for error in report["errors"]))

    def test_build_report_flags_module_ordering_cycle(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:module_order_cycle_specs"},
            clear=False,
        ):
            reset_settings_cache()

            report, exit_code = build_report(check_paths=False)

        self.assertEqual(exit_code, 1)
        self.assertTrue(any("module migration ordering cycle detected" in error for error in report["errors"]))

    def test_cli_outputs_json_and_checks_paths(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/validate_optional_module_persistence.py",
                "--check-paths",
                "--expect-module",
                "external_example_pack",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.external_module_pack.manifest:module_spec",
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["expectedModules"], ["external_example_pack"])
        self.assertIn("external_example_pack", {item["module"] for item in payload["modules"]})


if __name__ == "__main__":
    unittest.main()
