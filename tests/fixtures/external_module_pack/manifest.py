from __future__ import annotations

from app.modules.registry import OptionalModuleSpec


def module_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="external_example_pack",
        display_name="External Example Pack",
        canonical_namespace="tests.fixtures.external_module_pack",
        setting_name="external_example_pack_module_enabled",
        disabled_reason="External example pack disabled.",
        route_prefix="/api/external-example",
        route_prefixes=("/api/external-example",),
        route_factory_refs=("tests.fixtures.external_module_pack.routes:create_router",),
        read_scopes=("external_example:read",),
        write_scopes=("external_example:write",),
        frontend={
            "surfaceKey": "external_example",
            "showWhenAvailable": True,
            "branchPolicy": "frontend work belongs in a separate FE branch",
        },
        tool_names=("agency.external-example.read",),
        runtime_tool_names=("agency.external-example.read",),
        read_only_tool_names=("agency.external-example.read",),
        preferred_tool_names=("agency.external-example.read",),
        system_tool_family_key="external_example",
        system_tool_definition_builder_ref=(
            "tests.fixtures.external_module_pack.tool_definitions:external_example_system_tool_definitions"
        ),
        system_tool_id_builder_ref="tests.fixtures.external_module_pack.tool_definitions:external_example_system_tool_ids",
        runtime_tool_handler_ref="tests.fixtures.external_module_pack.runtime_tools:ExternalExampleRuntimeToolHandler",
        persistence_manifest_ref="tests.fixtures.external_module_pack.persistence:external_example_persistence_manifest",
        domain_manifest_ref="tests.fixtures.external_module_pack.domain:external_example_domain_manifest",
        observability_manifest_ref=(
            "tests.fixtures.external_module_pack.observability:external_example_observability_manifest"
        ),
        graph_manifest_ref="tests.fixtures.external_module_pack.graph:external_example_graph_manifest",
        package_name="agency-external-example-pack",
        package_version="0.1.0",
        install_hint="pip install agency-external-example-pack",
        presence_ref="tests.fixtures.external_module_pack",
        notes=(
            "Example external pack used to validate config-ref and entry-point loading.",
        ),
    )

