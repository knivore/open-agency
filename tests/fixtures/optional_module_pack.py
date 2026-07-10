from __future__ import annotations

from app.modules.registry import OptionalModuleSpec


def example_module_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="example_pack",
        display_name="Example Pack",
        canonical_namespace="tests.fixtures.optional_module_pack",
        setting_name="example_pack_module_enabled",
        disabled_reason="Example pack disabled.",
        route_prefix="/api/example-pack",
        route_prefixes=("/api/example-pack",),
        route_factory_refs=(),
        read_scopes=("example:read",),
        write_scopes=("example:write",),
        frontend={"surfaceKey": "example_pack", "showWhenAvailable": True},
        tool_names=("agency.example.read",),
        read_only_tool_names=("agency.example.read",),
        preferred_tool_names=("agency.example.read",),
        persistence_manifest_ref="tests.fixtures.optional_module_pack:example_persistence_manifest",
        package_name="agency-example-pack",
        package_version="0.1.0",
        install_hint="pip install agency-example-pack",
    )


def duplicate_smart_home_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="smart_home",
        display_name="Duplicate Smart Home",
        canonical_namespace="tests.fixtures.optional_module_pack.duplicate",
        setting_name="duplicate_smart_home_module_enabled",
        disabled_reason="Duplicate disabled.",
        route_prefix="/api/duplicate-smart-home",
        route_prefixes=("/api/duplicate-smart-home",),
        route_factory_refs=(),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "duplicate_smart_home"},
    )


def external_home_pack_specs() -> tuple[OptionalModuleSpec, ...]:
    return (
        OptionalModuleSpec(
            key="smart_home",
            display_name="External Smart Home",
            canonical_namespace="agency_smart_home_pack.smart_home",
            setting_name="smart_home_module_enabled",
            disabled_reason="External smart-home pack disabled.",
            route_prefix="/api/smart-home",
            route_prefixes=("/api/smart-home",),
            route_factory_refs=(),
            read_scopes=("integrations:read",),
            write_scopes=("integrations:write",),
            frontend={"surfaceKey": "smart_home", "showWhenAvailable": True},
            tool_names=("home_assistant.call_service",),
            vendor_specific_tool_names=("home_assistant.call_service",),
            package_name="agency-smart-home-pack",
            package_version="0.1.0",
            install_hint="pip install agency-smart-home-pack",
        ),
        OptionalModuleSpec(
            key="physical_devices",
            display_name="External Physical Devices",
            canonical_namespace="agency_physical_devices_pack.physical_devices",
            setting_name="physical_devices_module_enabled",
            disabled_reason="External physical-devices pack disabled.",
            route_prefix="/api/devices",
            route_prefixes=("/api/devices", "/api/device-events", "/api/physical/events", "/api/xr/simulator"),
            route_factory_refs=(),
            read_scopes=("integrations:read",),
            write_scopes=("integrations:write",),
            frontend={"surfaceKey": "physical_devices", "showWhenAvailable": True, "hideWhenUnavailable": True},
            tool_names=("agency.device.list", "agency.device.command", "agency.physical.event-bus.health"),
            read_only_tool_names=("agency.device.list", "agency.physical.event-bus.health"),
            mutating_tool_names=("agency.device.command",),
            preferred_tool_names=("agency.device.list", "agency.physical.event-bus.health"),
            extra_capabilities={"eventRoutePrefix": "/api/physical/events"},
            package_name="agency-physical-devices-pack",
            package_version="0.1.0",
            install_hint="pip install agency-physical-devices-pack",
        ),
    )


def multiple_module_specs() -> tuple[OptionalModuleSpec, ...]:
    return (example_module_spec(),)


def invalid_module_spec() -> dict[str, str]:
    return {"key": "invalid"}


def disabled_bad_route_module_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="disabled_bad_route_pack",
        display_name="Disabled Bad Route Pack",
        canonical_namespace="tests.fixtures.optional_module_pack.disabled_bad_route",
        setting_name="disabled_bad_route_pack_module_enabled",
        disabled_reason="Disabled bad route pack disabled.",
        route_prefix="/api/disabled-bad-route-pack",
        route_prefixes=("/api/disabled-bad-route-pack",),
        route_factory_refs=("tests.fixtures.missing_optional_module_routes:create_router",),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "disabled_bad_route_pack"},
    )


def disabled_bad_tool_builder_module_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="disabled_bad_tool_builder_pack",
        display_name="Disabled Bad Tool Builder Pack",
        canonical_namespace="tests.fixtures.optional_module_pack.disabled_bad_tool_builder",
        setting_name="disabled_bad_tool_builder_pack_module_enabled",
        disabled_reason="Disabled bad tool builder pack disabled.",
        route_prefix="/api/disabled-bad-tool-builder-pack",
        route_prefixes=("/api/disabled-bad-tool-builder-pack",),
        route_factory_refs=(),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "disabled_bad_tool_builder_pack"},
        system_tool_family_key="disabled_bad_tool_builder",
        system_tool_definition_builder_ref="tests.fixtures.missing_optional_module_tools:tool_definitions",
        system_tool_id_builder_ref="tests.fixtures.missing_optional_module_tools:tool_ids",
    )


def disabled_bad_manifest_module_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="disabled_bad_manifest_pack",
        display_name="Disabled Bad Manifest Pack",
        canonical_namespace="tests.fixtures.optional_module_pack.disabled_bad_manifest",
        setting_name="disabled_bad_manifest_pack_module_enabled",
        disabled_reason="Disabled bad manifest pack disabled.",
        route_prefix="/api/disabled-bad-manifest-pack",
        route_prefixes=("/api/disabled-bad-manifest-pack",),
        route_factory_refs=(),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "disabled_bad_manifest_pack"},
        runtime_tool_names=("agency.disabled-bad-manifest.read",),
        runtime_tool_handler_ref="tests.fixtures.missing_optional_module_manifests:RuntimeHandler",
        memory_service_ref="tests.fixtures.missing_optional_module_manifests:MemoryService",
        persistence_manifest_ref="tests.fixtures.missing_optional_module_manifests:persistence_manifest",
        domain_manifest_ref="tests.fixtures.missing_optional_module_manifests:domain_manifest",
        observability_manifest_ref="tests.fixtures.missing_optional_module_manifests:observability_manifest",
        graph_manifest_ref="tests.fixtures.missing_optional_module_manifests:graph_manifest",
        adapter_factory_ref="tests.fixtures.missing_optional_module_manifests:adapter_factory",
        context_repository_factory_ref="tests.fixtures.missing_optional_module_manifests:context_repository_factory",
        context_resolver_class_ref="tests.fixtures.missing_optional_module_manifests:ContextResolver",
    )


def example_persistence_manifest() -> dict[str, object]:
    return {
        "module": "example_pack",
        "orm_model_refs": ("tests.fixtures.optional_module_pack:ExampleORM",),
        "alembic_revisions": ("example_0001",),
        "alembic_version_paths": ("tests/fixtures/example_pack_migrations/example_0001.py",),
        "migration_source": "package",
        "removal_policy": "preserve_data",
        "removal_notes": ("Example pack test data is retained unless an operator drops the table.",),
        "tables": ("example_pack_items",),
    }


def mismatched_persistence_manifest_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="bad_pack",
        display_name="Bad Pack",
        canonical_namespace="tests.fixtures.optional_module_pack.bad",
        setting_name="bad_pack_module_enabled",
        disabled_reason="Bad pack disabled.",
        route_prefix="/api/bad-pack",
        route_prefixes=("/api/bad-pack",),
        route_factory_refs=(),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "bad_pack"},
        persistence_manifest_ref="tests.fixtures.optional_module_pack:mismatched_persistence_manifest",
    )


def mismatched_persistence_manifest() -> dict[str, object]:
    return {
        "module": "other_pack",
        "orm_model_refs": (),
        "alembic_revisions": (),
        "alembic_version_paths": (),
        "tables": (),
    }


def unpaired_migration_manifest_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="unpaired_pack",
        display_name="Unpaired Pack",
        canonical_namespace="tests.fixtures.optional_module_pack.unpaired",
        setting_name="unpaired_pack_module_enabled",
        disabled_reason="Unpaired pack disabled.",
        route_prefix="/api/unpaired-pack",
        route_prefixes=("/api/unpaired-pack",),
        route_factory_refs=(),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "unpaired_pack"},
        persistence_manifest_ref="tests.fixtures.optional_module_pack:unpaired_migration_manifest",
    )


def unpaired_migration_manifest() -> dict[str, object]:
    return {
        "module": "unpaired_pack",
        "orm_model_refs": (),
        "alembic_revisions": ("unpaired_0001",),
        "alembic_version_paths": (),
        "tables": (),
    }


def invalid_migration_source_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="invalid_source_pack",
        display_name="Invalid Source Pack",
        canonical_namespace="tests.fixtures.optional_module_pack.invalid_source",
        setting_name="invalid_source_pack_module_enabled",
        disabled_reason="Invalid source pack disabled.",
        route_prefix="/api/invalid-source-pack",
        route_prefixes=("/api/invalid-source-pack",),
        route_factory_refs=(),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "invalid_source_pack"},
        persistence_manifest_ref="tests.fixtures.optional_module_pack:invalid_migration_source_manifest",
    )


def invalid_migration_source_manifest() -> dict[str, object]:
    return {
        "module": "invalid_source_pack",
        "orm_model_refs": (),
        "alembic_revisions": (),
        "alembic_version_paths": (),
        "migration_source": "vendored",
        "tables": (),
    }


def overlapping_tool_metadata_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="overlapping_tool_metadata",
        display_name="Overlapping Tool Metadata",
        canonical_namespace="tests.fixtures.optional_module_pack.overlapping_tool_metadata",
        setting_name="overlapping_tool_metadata_module_enabled",
        disabled_reason="Overlapping tool metadata disabled.",
        route_prefix="/api/overlapping-tool-metadata",
        route_prefixes=("/api/overlapping-tool-metadata",),
        route_factory_refs=(),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "overlapping_tool_metadata"},
        tool_names=("home_assistant.call_service",),
        preferred_tool_names=("home_assistant.call_service",),
        vendor_specific_tool_names=("home_assistant.call_service",),
    )


def invalid_removal_policy_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="invalid_removal_pack",
        display_name="Invalid Removal Pack",
        canonical_namespace="tests.fixtures.optional_module_pack.invalid_removal",
        setting_name="invalid_removal_pack_module_enabled",
        disabled_reason="Invalid removal pack disabled.",
        route_prefix="/api/invalid-removal-pack",
        route_prefixes=("/api/invalid-removal-pack",),
        route_factory_refs=(),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "invalid_removal_pack"},
        persistence_manifest_ref="tests.fixtures.optional_module_pack:invalid_removal_policy_manifest",
    )


def invalid_removal_policy_manifest() -> dict[str, object]:
    return {
        "module": "invalid_removal_pack",
        "orm_model_refs": (),
        "alembic_revisions": (),
        "alembic_version_paths": (),
        "removal_policy": "unknown",
        "tables": (),
    }


def duplicate_revision_module_specs() -> tuple[OptionalModuleSpec, ...]:
    first = example_module_spec()
    second = OptionalModuleSpec(
        key="duplicate_revision_pack",
        display_name="Duplicate Revision Pack",
        canonical_namespace="tests.fixtures.optional_module_pack.duplicate_revision",
        setting_name="duplicate_revision_pack_module_enabled",
        disabled_reason="Duplicate revision pack disabled.",
        route_prefix="/api/duplicate-revision-pack",
        route_prefixes=("/api/duplicate-revision-pack",),
        route_factory_refs=(),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "duplicate_revision_pack"},
        persistence_manifest_ref="tests.fixtures.optional_module_pack:duplicate_revision_manifest",
    )
    return (first, second)


def duplicate_revision_manifest() -> dict[str, object]:
    return {
        "module": "duplicate_revision_pack",
        "orm_model_refs": (),
        "alembic_revisions": ("example_0001",),
        "alembic_version_paths": ("tests/fixtures/duplicate_revision_pack_migrations/example_0001.py",),
        "tables": (),
    }


def missing_dependency_module_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="missing_dependency_pack",
        display_name="Missing Dependency Pack",
        canonical_namespace="tests.fixtures.optional_module_pack.missing_dependency",
        setting_name="missing_dependency_pack_module_enabled",
        disabled_reason="Missing dependency pack disabled.",
        route_prefix="/api/missing-dependency-pack",
        route_prefixes=("/api/missing-dependency-pack",),
        route_factory_refs=(),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "missing_dependency_pack"},
        persistence_manifest_ref="tests.fixtures.optional_module_pack:missing_dependency_manifest",
    )


def missing_dependency_manifest() -> dict[str, object]:
    return {
        "module": "missing_dependency_pack",
        "orm_model_refs": (),
        "alembic_revisions": ("missing_dependency_0001",),
        "alembic_version_paths": ("tests/fixtures/missing_dependency_pack_migrations/0001.py",),
        "migration_dependencies": {"missing_dependency_0001": ("does_not_exist_0001",)},
        "tables": (),
    }


def cyclic_dependency_module_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="cyclic_dependency_pack",
        display_name="Cyclic Dependency Pack",
        canonical_namespace="tests.fixtures.optional_module_pack.cyclic_dependency",
        setting_name="cyclic_dependency_pack_module_enabled",
        disabled_reason="Cyclic dependency pack disabled.",
        route_prefix="/api/cyclic-dependency-pack",
        route_prefixes=("/api/cyclic-dependency-pack",),
        route_factory_refs=(),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "cyclic_dependency_pack"},
        persistence_manifest_ref="tests.fixtures.optional_module_pack:cyclic_dependency_manifest",
    )


def cyclic_dependency_manifest() -> dict[str, object]:
    return {
        "module": "cyclic_dependency_pack",
        "orm_model_refs": (),
        "alembic_revisions": ("cycle_0001", "cycle_0002"),
        "alembic_version_paths": (
            "tests/fixtures/cyclic_dependency_pack_migrations/0001.py",
            "tests/fixtures/cyclic_dependency_pack_migrations/0002.py",
        ),
        "migration_dependencies": {
            "cycle_0001": ("cycle_0002",),
            "cycle_0002": ("cycle_0001",),
        },
        "tables": (),
    }


def module_order_cycle_specs() -> tuple[OptionalModuleSpec, ...]:
    first = OptionalModuleSpec(
        key="module_cycle_a",
        display_name="Module Cycle A",
        canonical_namespace="tests.fixtures.optional_module_pack.module_cycle_a",
        setting_name="module_cycle_a_module_enabled",
        disabled_reason="Module cycle A disabled.",
        route_prefix="/api/module-cycle-a",
        route_prefixes=("/api/module-cycle-a",),
        route_factory_refs=(),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "module_cycle_a"},
        persistence_manifest_ref="tests.fixtures.optional_module_pack:module_cycle_a_manifest",
    )
    second = OptionalModuleSpec(
        key="module_cycle_b",
        display_name="Module Cycle B",
        canonical_namespace="tests.fixtures.optional_module_pack.module_cycle_b",
        setting_name="module_cycle_b_module_enabled",
        disabled_reason="Module cycle B disabled.",
        route_prefix="/api/module-cycle-b",
        route_prefixes=("/api/module-cycle-b",),
        route_factory_refs=(),
        read_scopes=(),
        write_scopes=(),
        frontend={"surfaceKey": "module_cycle_b"},
        persistence_manifest_ref="tests.fixtures.optional_module_pack:module_cycle_b_manifest",
    )
    return (first, second)


def module_cycle_a_manifest() -> dict[str, object]:
    return {
        "module": "module_cycle_a",
        "orm_model_refs": (),
        "alembic_revisions": ("module_cycle_a_0001",),
        "alembic_version_paths": ("tests/fixtures/module_cycle_a_migrations/0001.py",),
        "after_modules": ("module_cycle_b",),
        "tables": (),
    }


def module_cycle_b_manifest() -> dict[str, object]:
    return {
        "module": "module_cycle_b",
        "orm_model_refs": (),
        "alembic_revisions": ("module_cycle_b_0001",),
        "alembic_version_paths": ("tests/fixtures/module_cycle_b_migrations/0001.py",),
        "after_modules": ("module_cycle_a",),
        "tables": (),
    }


class ExampleORM:
    __tablename__ = "example_pack_items"
