from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.core.config import reset_settings_cache
from app.modules.registry import (
    OPTIONAL_MODULE_ENTRY_POINT_GROUP,
    optional_module_available,
    optional_module_alembic_version_locations,
    optional_module_adapter_factory,
    optional_module_capabilities,
    optional_module_context_repository_factory,
    optional_module_context_resolver_class,
    optional_module_domain_manifests,
    optional_module_graph_manifests,
    optional_module_memory_service_class,
    optional_module_observability_manifests,
    optional_module_persistence_plans,
    optional_module_route_factories,
    optional_module_runtime_tool_handler_class,
    optional_module_runtime_tool_names,
    optional_module_specs,
    optional_module_system_tool_family_builders,
    validate_expected_optional_modules,
)


class _FakeEntryPoint:
    group = OPTIONAL_MODULE_ENTRY_POINT_GROUP

    def __init__(self, name: str, loader):
        self.name = name
        self._loader = loader

    def load(self):
        return self._loader()


def _fixture_attr(name: str):
    return getattr(__import__("tests.fixtures.optional_module_pack", fromlist=[name]), name)


def _fixture_attr_from(module_name: str, name: str):
    return getattr(__import__(module_name, fromlist=[name]), name)


class OptionalModuleRegistryTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_settings_cache()

    def test_configured_module_spec_ref_is_loaded(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:example_module_spec"},
            clear=False,
        ):
            reset_settings_cache()

            specs = optional_module_specs()
            capabilities = optional_module_capabilities(visible_tool_names=lambda names: names)

        self.assertIn("example_pack", {spec.key for spec in specs})
        self.assertTrue(capabilities["example_pack"]["available"])
        self.assertEqual(capabilities["example_pack"]["tools"]["readOnly"], ["agency.example.read"])
        self.assertEqual(
            capabilities["example_pack"]["moduleLoading"],
            {
                "entryPointGroup": OPTIONAL_MODULE_ENTRY_POINT_GROUP,
                "configRefEnv": "AGENCY_OPTIONAL_MODULE_SPEC_REFS",
                "expectedModulesEnv": "AGENCY_EXPECTED_OPTIONAL_MODULES",
                "settingName": "example_pack_module_enabled",
            },
        )
        self.assertEqual(
            capabilities["example_pack"]["package"],
            {
                "name": "agency-example-pack",
                "version": "0.1.0",
                "installHint": "pip install agency-example-pack",
            },
        )

    def test_entry_point_module_spec_is_loaded(self) -> None:
        entry_point = _FakeEntryPoint("example-pack", lambda: _fixture_attr("example_module_spec"))
        with patch.dict(os.environ, {"AGENCY_OPTIONAL_MODULE_ENTRY_POINTS_ENABLED": "true"}, clear=False), patch(
            "app.modules.registry.entry_points",
            return_value=[entry_point],
        ):
            reset_settings_cache()
            specs = optional_module_specs()
            capabilities = optional_module_capabilities(visible_tool_names=lambda names: names)

        self.assertEqual(entry_point.group, OPTIONAL_MODULE_ENTRY_POINT_GROUP)
        self.assertIn("example_pack", {spec.key for spec in specs})
        self.assertTrue(capabilities["example_pack"]["available"])
        self.assertEqual(
            capabilities["example_pack"]["moduleLoading"]["entryPointGroup"],
            OPTIONAL_MODULE_ENTRY_POINT_GROUP,
        )

    def test_entry_point_module_specs_are_disabled_by_default(self) -> None:
        entry_point = _FakeEntryPoint("example-pack", lambda: _fixture_attr("example_module_spec"))
        with patch.dict(os.environ, {"AGENCY_OPTIONAL_MODULE_ENTRY_POINTS_ENABLED": "false"}, clear=False), patch(
            "app.modules.registry.entry_points",
            return_value=[entry_point],
        ):
            reset_settings_cache()
            specs = optional_module_specs()

        self.assertNotIn("example_pack", {spec.key for spec in specs})

    def test_entry_point_can_return_multiple_specs(self) -> None:
        entry_point = _FakeEntryPoint("example-packs", lambda: _fixture_attr("multiple_module_specs"))
        with patch.dict(os.environ, {"AGENCY_OPTIONAL_MODULE_ENTRY_POINTS_ENABLED": "true"}, clear=False), patch(
            "app.modules.registry.entry_points",
            return_value=[entry_point],
        ):
            reset_settings_cache()
            specs = optional_module_specs()

        self.assertIn("example_pack", {spec.key for spec in specs})

    def test_entry_point_specs_are_loaded_without_core_builtins(self) -> None:
        entry_point = _FakeEntryPoint("home-pack", lambda: _fixture_attr("external_home_pack_specs"))
        with patch.dict(os.environ, {"AGENCY_OPTIONAL_MODULE_ENTRY_POINTS_ENABLED": "true"}, clear=False), patch(
            "app.modules.registry.entry_points",
            return_value=[entry_point],
        ):
            reset_settings_cache()
            specs = optional_module_specs()

        specs_by_key = {spec.key: spec for spec in specs}
        self.assertEqual(specs_by_key["smart_home"].canonical_namespace, "agency_smart_home_pack.smart_home")
        self.assertEqual(
            specs_by_key["physical_devices"].canonical_namespace,
            "agency_physical_devices_pack.physical_devices",
        )

    def test_entry_point_duplicate_module_key_fails_fast_when_builtin_is_disabled(self) -> None:
        duplicate_entry_point = _FakeEntryPoint(
            "duplicate-smart-home",
            lambda: _fixture_attr("duplicate_smart_home_spec"),
        )
        home_pack_entry_point = _FakeEntryPoint("home-pack", lambda: _fixture_attr("external_home_pack_specs"))
        with patch.dict(
            os.environ,
            {"AGENCY_BUILTIN_OPTIONAL_MODULES": "", "AGENCY_OPTIONAL_MODULE_ENTRY_POINTS_ENABLED": "true"},
            clear=False,
        ):
            reset_settings_cache()
            with patch("app.modules.registry.entry_points", return_value=[duplicate_entry_point, home_pack_entry_point]):
                with self.assertRaisesRegex(RuntimeError, "Duplicate optional module key 'smart_home'"):
                    optional_module_specs()

    def test_config_ref_external_home_pack_loads_when_core_has_no_builtins(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:external_home_pack_specs"},
            clear=False,
        ):
            reset_settings_cache()

            specs = optional_module_specs()

        self.assertEqual({spec.key for spec in specs}, {"smart_home", "physical_devices"})

    def test_config_ref_overrides_same_entry_point_module_key(self) -> None:
        entry_point = _FakeEntryPoint("home-pack", lambda: _fixture_attr("external_home_pack_specs"))
        with patch.dict(
            os.environ,
            {
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:external_home_pack_specs",
                "AGENCY_OPTIONAL_MODULE_ENTRY_POINTS_ENABLED": "true",
            },
            clear=False,
        ), patch("app.modules.registry.entry_points", return_value=[entry_point]):
            reset_settings_cache()

            specs = optional_module_specs()

        self.assertEqual({spec.key for spec in specs}, {"smart_home", "physical_devices"})

    def test_builtin_optional_module_specs_can_be_replaced_by_external_home_pack(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENCY_BUILTIN_OPTIONAL_MODULES": "",
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:external_home_pack_specs",
            },
            clear=False,
        ), patch("app.modules.registry.entry_points", return_value=[]):
            reset_settings_cache()

            specs = optional_module_specs()
            capabilities = optional_module_capabilities(visible_tool_names=lambda names: names)

        self.assertEqual({spec.key for spec in specs}, {"smart_home", "physical_devices"})
        self.assertEqual(capabilities["smart_home"]["canonicalNamespace"], "agency_smart_home_pack.smart_home")
        self.assertEqual(
            capabilities["physical_devices"]["canonicalNamespace"],
            "agency_physical_devices_pack.physical_devices",
        )
        self.assertEqual(capabilities["smart_home"]["package"]["name"], "agency-smart-home-pack")
        self.assertEqual(capabilities["physical_devices"]["package"]["name"], "agency-physical-devices-pack")

    def test_unknown_builtin_optional_module_key_fails_fast(self) -> None:
        with patch.dict(os.environ, {"AGENCY_BUILTIN_OPTIONAL_MODULES": "smart_home,missing_builtin"}, clear=False):
            reset_settings_cache()

            with self.assertRaisesRegex(RuntimeError, "unknown module keys: missing_builtin, smart_home"):
                optional_module_specs()

    def test_external_style_pack_loads_through_config_ref(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.external_module_pack.manifest:module_spec"},
            clear=False,
        ):
            reset_settings_cache()

            capabilities = optional_module_capabilities(visible_tool_names=lambda names: names)
            route_factories = optional_module_route_factories()
            builders = optional_module_system_tool_family_builders()
            runtime_tool_names = optional_module_runtime_tool_names("external_example_pack")
            runtime_handler = optional_module_runtime_tool_handler_class("external_example_pack")
            persistence_plans = optional_module_persistence_plans()
            domain_manifests = optional_module_domain_manifests()
            observability_manifests = optional_module_observability_manifests()
            graph_manifests = optional_module_graph_manifests()

        external_capabilities = capabilities["external_example_pack"]
        self.assertTrue(external_capabilities["available"])
        self.assertEqual(external_capabilities["package"]["name"], "agency-external-example-pack")
        self.assertEqual(external_capabilities["frontend"]["surfaceKey"], "external_example")
        self.assertEqual(external_capabilities["tools"]["readOnly"], ["agency.external-example.read"])
        self.assertIn("agency.external-example.read", runtime_tool_names)
        self.assertEqual(runtime_handler.__name__, "ExternalExampleRuntimeToolHandler")
        self.assertTrue(
            any(factory.__module__ == "tests.fixtures.external_module_pack.routes" for factory in route_factories)
        )
        self.assertIn("external_example", builders)
        tool_definitions, tool_ids = builders["external_example"]
        owned_definitions = tool_definitions(True)
        self.assertEqual([tool.id for tool in owned_definitions], ["agency.external-example.read"])
        self.assertEqual(
            owned_definitions[0].implementation.config["agency_optional_module_key"],
            "external_example_pack",
        )
        self.assertEqual(tool_ids(True), ["agency.external-example.read"])
        self.assertEqual(tool_definitions(False), [])
        self.assertEqual(tool_ids(False), [])

        plan = persistence_plans["external_example_pack"]
        self.assertEqual(plan.migration_source, "package")
        self.assertEqual(plan.removal_policy, "preserve_data")
        self.assertEqual(plan.migrations[0].version_location, "tests/fixtures/external_module_pack/migrations")
        self.assertEqual(
            domain_manifests["external_example_pack"]["model_refs"],
            ("tests.fixtures.external_module_pack.domain:ExternalExampleCommand",),
        )
        self.assertIn("audit", observability_manifests["external_example_pack"]["hook_refs"])
        self.assertIn(
            "tests.fixtures.external_module_pack.graph:append_external_example_delta",
            graph_manifests["external_example_pack"]["delta_builder_refs"],
        )

    def test_external_pack_satisfies_install_enable_contract(self) -> None:
        config_ref_env = {
            "AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.external_module_pack.manifest:module_spec",
        }
        with patch.dict(os.environ, config_ref_env, clear=False):
            reset_settings_cache()

            capabilities = optional_module_capabilities(visible_tool_names=lambda names: names)
            route_factories = optional_module_route_factories()
            runtime_tool_names = optional_module_runtime_tool_names("external_example_pack")
            runtime_handler = optional_module_runtime_tool_handler_class("external_example_pack")
            persistence_plans = optional_module_persistence_plans()
            domain_manifests = optional_module_domain_manifests()
            graph_manifests = optional_module_graph_manifests()
            observability_manifests = optional_module_observability_manifests()

        enabled = capabilities["external_example_pack"]
        self.assertTrue(enabled["available"])
        self.assertEqual(enabled["status"], "available")
        self.assertEqual(enabled["frontend"]["surfaceKey"], "external_example")
        self.assertTrue(enabled["frontend"]["showWhenAvailable"])
        self.assertEqual(enabled["package"]["installHint"], "pip install agency-external-example-pack")
        self.assertEqual(enabled["hiddenWhenUnavailable"]["routePrefixes"], ["/api/external-example"])
        self.assertEqual(enabled["hiddenWhenUnavailable"]["toolNames"], ["agency.external-example.read"])
        self.assertEqual(enabled["tools"]["readOnly"], ["agency.external-example.read"])
        self.assertEqual(enabled["tools"]["preferred"], ["agency.external-example.read"])
        self.assertTrue(
            any(factory.__module__ == "tests.fixtures.external_module_pack.routes" for factory in route_factories)
        )
        self.assertEqual(runtime_tool_names, {"agency.external-example.read"})
        self.assertEqual(runtime_handler.__name__, "ExternalExampleRuntimeToolHandler")
        self.assertIn("external_example_pack", persistence_plans)
        self.assertIn("external_example_pack", domain_manifests)
        self.assertIn("external_example_pack", graph_manifests)
        self.assertIn("external_example_pack", observability_manifests)

        with patch.dict(
            os.environ,
            {
                **config_ref_env,
                "EXTERNAL_EXAMPLE_PACK_MODULE_ENABLED": "false",
            },
            clear=False,
        ):
            reset_settings_cache()

            disabled_capabilities = optional_module_capabilities(visible_tool_names=lambda names: names)
            disabled_route_factories = optional_module_route_factories()
            disabled_runtime_names = optional_module_runtime_tool_names("external_example_pack")
            disabled_handler = optional_module_runtime_tool_handler_class("external_example_pack")
            disabled_plans = optional_module_persistence_plans()

        disabled = disabled_capabilities["external_example_pack"]
        self.assertFalse(disabled["available"])
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(disabled["reason"], "External example pack disabled.")
        self.assertFalse(
            any(
                factory.__module__ == "tests.fixtures.external_module_pack.routes"
                for factory in disabled_route_factories
            )
        )
        self.assertEqual(disabled_runtime_names, set())
        self.assertIsNone(disabled_handler)
        self.assertNotIn("external_example_pack", disabled_plans)

        def fake_find_spec(module_name: str):
            if module_name == "tests.fixtures.external_module_pack":
                return None
            return object()

        with patch.dict(os.environ, config_ref_env, clear=False):
            with patch("app.modules.registry.find_spec", side_effect=fake_find_spec):
                reset_settings_cache()

                missing_capabilities = optional_module_capabilities(visible_tool_names=lambda names: names)
                missing_route_factories = optional_module_route_factories()
                missing_errors = validate_expected_optional_modules(("external_example_pack",))

        missing = missing_capabilities["external_example_pack"]
        self.assertFalse(missing["available"])
        self.assertEqual(missing["reason"], "External Example Pack module package is not installed.")
        self.assertFalse(
            any(
                factory.__module__ == "tests.fixtures.external_module_pack.routes"
                for factory in missing_route_factories
            )
        )
        self.assertTrue(
            any(
                "expected optional module 'external_example_pack' is registered but unavailable" in error
                for error in missing_errors
            )
        )

    def test_external_style_pack_loads_through_entry_point(self) -> None:
        entry_point = _FakeEntryPoint(
            "external-example-pack",
            lambda: _fixture_attr_from("tests.fixtures.external_module_pack.manifest", "module_spec"),
        )
        with patch.dict(os.environ, {"AGENCY_OPTIONAL_MODULE_ENTRY_POINTS_ENABLED": "true"}, clear=False), patch(
            "app.modules.registry.entry_points",
            return_value=[entry_point],
        ):
            reset_settings_cache()
            specs = optional_module_specs()
            capabilities = optional_module_capabilities(visible_tool_names=lambda names: names)

        self.assertIn("external_example_pack", {spec.key for spec in specs})
        self.assertTrue(capabilities["external_example_pack"]["available"])
        self.assertEqual(
            capabilities["external_example_pack"]["moduleLoading"]["entryPointGroup"],
            OPTIONAL_MODULE_ENTRY_POINT_GROUP,
        )

    def test_configured_module_persistence_plan_is_loaded(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:example_module_spec"},
            clear=False,
        ):
            reset_settings_cache()

            plans = optional_module_persistence_plans()
            locations = optional_module_alembic_version_locations()

        self.assertIn("example_pack", plans)
        self.assertEqual(plans["example_pack"].migrations[0].revision, "example_0001")
        self.assertEqual(plans["example_pack"].migration_source, "package")
        self.assertEqual(plans["example_pack"].removal_policy, "preserve_data")
        self.assertEqual(
            plans["example_pack"].removal_notes,
            ("Example pack test data is retained unless an operator drops the table.",),
        )
        self.assertIn("tests/fixtures/example_pack_migrations", locations)

    def test_configured_module_spec_ref_can_return_multiple_specs(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:multiple_module_specs"},
            clear=False,
        ):
            reset_settings_cache()

            specs = optional_module_specs()

        self.assertIn("example_pack", {spec.key for spec in specs})

    def test_configured_module_can_be_disabled_by_conventional_env_flag(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:example_module_spec",
                "EXAMPLE_PACK_MODULE_ENABLED": "false",
            },
            clear=False,
        ):
            reset_settings_cache()

            capabilities = optional_module_capabilities(visible_tool_names=lambda names: names)

        self.assertFalse(capabilities["example_pack"]["available"])
        self.assertEqual(capabilities["example_pack"]["status"], "disabled")
        self.assertEqual(capabilities["example_pack"]["reason"], "Example pack disabled.")

    def test_expected_module_validation_accepts_available_modules(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:external_home_pack_specs"},
            clear=False,
        ):
            reset_settings_cache()
            errors = validate_expected_optional_modules(("smart_home", "physical_devices"))

        self.assertEqual(errors, ())

    def test_core_only_runtime_has_no_home_pack_modules(self) -> None:
        with patch("app.modules.registry.entry_points", return_value=[]):
            capabilities = optional_module_capabilities(visible_tool_names=lambda names: names)
            route_factories = optional_module_route_factories()
            runtime_tool_names = optional_module_runtime_tool_names()
            errors = validate_expected_optional_modules(("smart_home", "physical_devices"))

        self.assertNotIn("smart_home", capabilities)
        self.assertNotIn("physical_devices", capabilities)
        self.assertEqual(route_factories, [])
        self.assertEqual(runtime_tool_names, set())
        self.assertTrue(any("expected optional module 'smart_home' is not registered" in error for error in errors))
        self.assertTrue(any("expected optional module 'physical_devices' is not registered" in error for error in errors))

    def test_expected_module_validation_flags_missing_and_disabled_modules(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:external_home_pack_specs",
                "SMART_HOME_MODULE_ENABLED": "false",
            },
            clear=False,
        ):
            reset_settings_cache()

            errors = validate_expected_optional_modules(("smart_home", "missing_pack"))

        self.assertTrue(any("expected optional module 'smart_home' is registered but unavailable" in error for error in errors))
        self.assertTrue(any("expected optional module 'missing_pack' is not registered" in error for error in errors))

    def test_disabled_module_route_factories_are_not_imported(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": (
                    "tests.fixtures.optional_module_pack:disabled_bad_route_module_spec"
                ),
                "DISABLED_BAD_ROUTE_PACK_MODULE_ENABLED": "false",
            },
            clear=False,
        ):
            reset_settings_cache()

            factories = optional_module_route_factories()
            capabilities = optional_module_capabilities(visible_tool_names=lambda names: names)

        self.assertIsInstance(factories, list)
        self.assertFalse(capabilities["disabled_bad_route_pack"]["available"])

    def test_disabled_module_system_tool_builders_are_not_imported(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": (
                    "tests.fixtures.optional_module_pack:disabled_bad_tool_builder_module_spec"
                ),
                "DISABLED_BAD_TOOL_BUILDER_PACK_MODULE_ENABLED": "false",
            },
            clear=False,
        ):
            reset_settings_cache()

            builders = optional_module_system_tool_family_builders()
            capabilities = optional_module_capabilities(visible_tool_names=lambda names: names)

        self.assertNotIn("disabled_bad_tool_builder", builders)
        self.assertFalse(capabilities["disabled_bad_tool_builder_pack"]["available"])

    def test_enabled_module_system_tool_builder_import_errors_fail_fast(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": (
                    "tests.fixtures.optional_module_pack:disabled_bad_tool_builder_module_spec"
                ),
                "DISABLED_BAD_TOOL_BUILDER_PACK_MODULE_ENABLED": "true",
            },
            clear=False,
        ):
            reset_settings_cache()

            with self.assertRaises(ModuleNotFoundError):
                optional_module_system_tool_family_builders()

    def test_disabled_module_manifests_and_handlers_are_not_imported(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": (
                    "tests.fixtures.optional_module_pack:disabled_bad_manifest_module_spec"
                ),
                "DISABLED_BAD_MANIFEST_PACK_MODULE_ENABLED": "false",
            },
            clear=False,
        ):
            reset_settings_cache()

            persistence_plans = optional_module_persistence_plans()
            domain_manifests = optional_module_domain_manifests()
            observability_manifests = optional_module_observability_manifests()
            graph_manifests = optional_module_graph_manifests()
            runtime_tool_names = optional_module_runtime_tool_names("disabled_bad_manifest_pack")
            runtime_handler = optional_module_runtime_tool_handler_class("disabled_bad_manifest_pack")
            memory_service = optional_module_memory_service_class("disabled_bad_manifest_pack")
            adapter_factory = optional_module_adapter_factory("disabled_bad_manifest_pack")
            repository_factory = optional_module_context_repository_factory("disabled_bad_manifest_pack")
            resolver_class = optional_module_context_resolver_class("disabled_bad_manifest_pack")

        self.assertNotIn("disabled_bad_manifest_pack", persistence_plans)
        self.assertNotIn("disabled_bad_manifest_pack", domain_manifests)
        self.assertNotIn("disabled_bad_manifest_pack", observability_manifests)
        self.assertNotIn("disabled_bad_manifest_pack", graph_manifests)
        self.assertEqual(runtime_tool_names, set())
        self.assertIsNone(runtime_handler)
        self.assertIsNone(memory_service)
        self.assertIsNone(adapter_factory)
        self.assertIsNone(repository_factory)
        self.assertIsNone(resolver_class)

    def test_invalid_configured_module_ref_fails_fast(self) -> None:
        with patch.dict(os.environ, {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "missing_colon"}, clear=False):
            reset_settings_cache()

            with self.assertRaisesRegex(RuntimeError, "module:attribute"):
                optional_module_specs()

    def test_configured_module_ref_must_return_specs(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:invalid_module_spec"},
            clear=False,
        ):
            reset_settings_cache()

            with self.assertRaisesRegex(TypeError, "OptionalModuleSpec"):
                optional_module_specs()

    def test_duplicate_configured_module_key_fails_fast(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": (
                    "tests.fixtures.optional_module_pack:external_home_pack_specs,"
                    "tests.fixtures.optional_module_pack:duplicate_smart_home_spec"
                )
            },
            clear=False,
        ):
            reset_settings_cache()

            with self.assertRaisesRegex(RuntimeError, "Duplicate optional module key 'smart_home'"):
                optional_module_specs()

    def test_configured_module_persistence_manifest_must_match_module_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": (
                    "tests.fixtures.optional_module_pack:mismatched_persistence_manifest_spec"
                )
            },
            clear=False,
        ):
            reset_settings_cache()

            with self.assertRaisesRegex(RuntimeError, "must declare module='bad_pack'"):
                optional_module_persistence_plans()

    def test_configured_module_persistence_manifest_must_pair_revisions_and_paths(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENCY_OPTIONAL_MODULE_SPEC_REFS": (
                    "tests.fixtures.optional_module_pack:unpaired_migration_manifest_spec"
                )
            },
            clear=False,
        ):
            reset_settings_cache()

            with self.assertRaisesRegex(RuntimeError, "pair each Alembic revision"):
                optional_module_persistence_plans()

    def test_configured_module_persistence_manifest_must_use_known_migration_source(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:invalid_migration_source_spec"},
            clear=False,
        ):
            reset_settings_cache()

            with self.assertRaisesRegex(RuntimeError, "migration_source must be 'core' or 'package'"):
                optional_module_persistence_plans()

    def test_configured_module_persistence_manifest_must_use_known_removal_policy(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENCY_OPTIONAL_MODULE_SPEC_REFS": "tests.fixtures.optional_module_pack:invalid_removal_policy_spec"},
            clear=False,
        ):
            reset_settings_cache()

            with self.assertRaisesRegex(RuntimeError, "removal_policy must be"):
                optional_module_persistence_plans()
