# Agency Optional Modules

This folder is Agency core's optional-module host boundary. It should teach
developers and AI agents how to make Agency discover, enable, disable, and
hide add-on modules without making the core backend depend on those modules.

Module-specific implementation docs belong in the module's own repository or
external module workspace, not in this folder. Agency core should keep only
generic extension guidance here.

## Core Contract

Agency core owns the module host, not module implementation code.

- `app/modules/registry.py` defines the generic `OptionalModuleSpec` contract
  and registry helpers.
- `AGENCY_OPTIONAL_MODULE_SPEC_REFS` loads private/local module specs by
  `module:attribute` reference.
- Python entry points in the `agency.module_packs` group load installable module
  packs only when `AGENCY_OPTIONAL_MODULE_ENTRY_POINTS_ENABLED=true`.
- `/capabilities.modules` tells clients which modules are available, disabled,
  or missing.
- `scripts/validate_optional_module_persistence.py` validates module-owned ORM
  and migration metadata without applying migrations.

The core backend must run when no optional modules are installed.

## Recommended Pack Shape

Prefer external Python packages for optional modules:

```text
agency_example_pack/
  pyproject.toml
  agency_example_pack/
    __init__.py
    manifest.py
    routes.py
    tool_definitions.py
    runtime_tools.py
    persistence.py
    domain.py
    migrations/
      versions/
        20260701_0001_example.py
```

Only create files the module actually needs. Keep vendor SDKs, adapter code,
module services, module tests, and module-specific docs inside the module repo.

## Minimal Module Spec

Every module exposes one `OptionalModuleSpec`.

```python
from app.modules.registry import OptionalModuleSpec


def module_spec() -> OptionalModuleSpec:
    return OptionalModuleSpec(
        key="example_pack",
        display_name="Example Pack",
        canonical_namespace="agency_example_pack",
        setting_name="example_pack_module_enabled",
        disabled_reason="Example pack disabled.",
        route_prefix="/api/example-pack",
        route_prefixes=("/api/example-pack",),
        route_factory_refs=("agency_example_pack.routes:create_router",),
        read_scopes=("example:read",),
        write_scopes=("example:write",),
        frontend={"surfaceKey": "example_pack", "showWhenAvailable": True},
        presence_ref="agency_example_pack",
        package_name="agency-example-pack",
        package_version="0.1.0",
        install_hint="pip install agency-example-pack",
    )
```

For local development:

```bash
AGENCY_OPTIONAL_MODULE_SPEC_REFS=agency_example_pack.manifest:module_spec
```

For installable packages:

```toml
[project.entry-points."agency.module_packs"]
example_pack = "agency_example_pack.manifest:module_spec"
```

Then enable entry-point discovery explicitly:

```bash
AGENCY_OPTIONAL_MODULE_ENTRY_POINTS_ENABLED=true
```

## What The Spec Can Declare

Use only the fields your module needs.

- `key`: stable module key used by Agency, clients, and tests.
- `setting_name`: boolean settings flag that can disable the module.
- `presence_ref`: importable module/package root used to report not-installed
  versus disabled.
- `route_factory_refs`: lazy route factory refs loaded only when available.
- `tool_names`: all tools owned by the module.
- `read_only_tool_names`, `mutating_tool_names`, `preferred_tool_names`, and
  `vendor_specific_tool_names`: tool metadata for agents and frontend gating.
- `system_tool_definition_builder_ref` and `system_tool_id_builder_ref`: lazy
  refs for system-tool registration.
- `runtime_tool_names` and `runtime_tool_handler_ref`: runtime execution seam
  for module-owned tools.
- `persistence_manifest_ref`: ORM, table, and Alembic migration ownership.
- `domain_manifest_ref`: module-owned domain contract references.
- `observability_manifest_ref`: audit/diagnostic hooks.
- `graph_manifest_ref`: graph stream and Neo4j projection hooks.
- `memory_service_ref`: module-specific memory behavior.
- `service_refs` and `exception_refs`: named service/exception refs for rare
  core integration seams.
- `frontend`: FE-visible metadata for capability gating.
- `package_name`, `package_version`, `install_hint`, `notes`, and
  `extra_capabilities`: operator/client metadata.

## Persistence Manifest

If a module owns tables, expose a persistence manifest from the module package:

```python
def persistence_manifest() -> dict:
    return {
        "module": "example_pack",
        "orm_model_refs": ("agency_example_pack.orm:ExampleORM",),
        "tables": ("example_records",),
        "alembic_revisions": ("20260701_0001_example",),
        "alembic_version_paths": (
            "agency_example_pack/migrations/versions/20260701_0001_example.py",
        ),
        "migration_source": "package",
        "removal_policy": "preserve_data",
        "removal_notes": (
            "Uninstalling the pack leaves data until an operator archives or drops it.",
        ),
    }
```

Validate it from Agency core:

```bash
AGENCY_OPTIONAL_MODULE_SPEC_REFS=agency_example_pack.manifest:module_spec \
./.venv/bin/python scripts/validate_optional_module_persistence.py \
  --check-paths \
  --expect-module example_pack
```

## Frontend And Client Gating

Clients must not infer module availability from routes or branch names. Read
`GET /capabilities` and use `modules`.

Hide a module surface when:

- The module key is absent.
- `modules.<key>.available` is false.
- Required route prefixes are listed under
  `modules.<key>.hiddenWhenUnavailable.routePrefixes`.
- Required tools are absent from the visible tool lists.

Missing module keys and disabled module keys should both produce a safe hidden
state in frontend/admin clients.

## AI Agent Rules

AI agents working on optional modules should follow these constraints:

- Do not add module implementation code to Agency core unless the feature is
  intentionally core-owned.
- Do not create compatibility alias packages to soften renames.
- Do not import module internals from core code. Add or reuse a registry seam.
- Keep route factories, runtime handlers, ORM models, migrations, tools,
  provider clients, and vendor SDK usage inside the module package.
- Keep module-specific docs in the module repository.
- Add tests for both installed/enabled and absent/disabled states.
- Keep mutating tools explicit in `mutating_tool_names` and enforce policy in
  the module.

## When To Change `registry.py`

Most new modules should not require registry changes. Add fields or helpers only
when a genuinely new generic extension seam is needed across modules.

Acceptable registry changes:

- A new lazy manifest type used by multiple modules.
- A new validation rule for all module specs or persistence manifests.
- A new generic helper used by core startup, route composition, tools,
  migrations, graph, observability, or frontend capability metadata.

Avoid registry changes for:

- One module's private service API.
- Vendor-specific behavior.
- Compatibility wrappers for old module names.
- Hardcoded module keys.

## Acceptance Checklist

Before claiming a module is Agency-compatible:

- Agency core starts with the module absent.
- Agency core starts with the module disabled.
- The module loads via `AGENCY_OPTIONAL_MODULE_SPEC_REFS`.
- The module loads via `agency.module_packs` entry point if installable.
- `/capabilities.modules` exposes accurate route/tool/frontend metadata.
- Disabled modules do not import route factories, runtime handlers, provider
  dependencies, or module-owned SDKs.
- Persistence validation passes with `--check-paths`.
- Module-owned tests live in the module repository.
- Core tests cover only the generic registry and capability behavior.
