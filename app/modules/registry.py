"""Optional backend module registry.

This file is the seam between Agency core and add-on style modules. Core
surfaces should ask this registry which optional routes, tools, and capability
metadata exist instead of importing module internals directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import entry_points
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable

from app.core.config import get_settings

RouteFactory = Callable[[Any], Any]
OPTIONAL_MODULE_ENTRY_POINT_GROUP = "agency.module_packs"
OPTIONAL_MODULE_TOOL_OWNER_CONFIG_KEY = "agency_optional_module_key"


@dataclass(frozen=True)
class OptionalModuleMigration:
    module_key: str
    revision: str
    path: str
    version_location: str
    depends_on_revisions: tuple[str, ...] = ()
    after_modules: tuple[str, ...] = ()


@dataclass(frozen=True)
class OptionalModulePersistencePlan:
    module_key: str
    orm_model_refs: tuple[str, ...]
    migrations: tuple[OptionalModuleMigration, ...]
    tables: tuple[str, ...]
    managed_by: str = "alembic"
    migration_source: str = "core"
    removal_policy: str = "manual"
    removal_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OptionalModuleSpec:
    key: str
    display_name: str
    canonical_namespace: str
    setting_name: str
    disabled_reason: str
    route_prefix: str
    route_prefixes: tuple[str, ...]
    route_factory_refs: tuple[str, ...]
    read_scopes: tuple[str, ...]
    write_scopes: tuple[str, ...]
    frontend: dict[str, Any]
    tool_names: tuple[str, ...] = ()
    runtime_tool_names: tuple[str, ...] = ()
    runtime_tool_handler_ref: str | None = None
    read_only_tool_names: tuple[str, ...] = ()
    mutating_tool_names: tuple[str, ...] = ()
    preferred_tool_names: tuple[str, ...] = ()
    vendor_specific_tool_names: tuple[str, ...] = ()
    system_tool_family_key: str | None = None
    system_tool_definition_builder_ref: str | None = None
    system_tool_id_builder_ref: str | None = None
    agent_tool_profile_builder_ref: str | None = None
    system_tool_enabled_kwarg: str = "enabled"
    persistence_manifest_ref: str | None = None
    domain_manifest_ref: str | None = None
    observability_manifest_ref: str | None = None
    graph_manifest_ref: str | None = None
    memory_service_ref: str | None = None
    service_refs: dict[str, str] | None = None
    exception_refs: dict[str, str] | None = None
    adapter_factory_ref: str | None = None
    context_repository_factory_ref: str | None = None
    context_resolver_class_ref: str | None = None
    package_name: str | None = None
    package_version: str | None = None
    install_hint: str | None = None
    presence_ref: str | None = None
    notes: tuple[str, ...] = ()
    extra_capabilities: dict[str, Any] | None = None

    def available(self) -> bool:
        settings = get_settings()
        configured = getattr(settings, self.setting_name, None)
        if configured is not None:
            return bool(configured) and self.installed()
        env_name = self.setting_name.upper()
        raw = os.getenv(env_name)
        if raw is not None and not _parse_bool_env(raw, env_name=env_name):
            return False
        return self.installed()

    def installed(self) -> bool:
        if not self.presence_ref:
            return True
        return _module_ref_importable(self.presence_ref)

    def unavailable_reason(self) -> str:
        if not self.installed():
            return f"{self.display_name} module package is not installed."
        return self.disabled_reason

    def load_route_factories(self) -> list[RouteFactory]:
        factories: list[RouteFactory] = []
        for ref in self.route_factory_refs:
            module_name, function_name = ref.split(":", 1)
            factories.append(getattr(import_module(module_name), function_name))
        return factories

    def load_system_tool_builders(self) -> tuple[Callable[[bool], list[Any]], Callable[[bool], list[str]]] | None:
        if (
                not self.system_tool_family_key
                or not self.system_tool_definition_builder_ref
                or not self.system_tool_id_builder_ref
        ):
            return None
        definition_builder = _load_ref(self.system_tool_definition_builder_ref)
        id_builder = _load_ref(self.system_tool_id_builder_ref)

        # Tool families still use legacy keyword names. The registry adapts that
        # detail so core catalog assembly only sees a consistent bool contract.
        def definitions(enabled: bool) -> list[Any]:
            tools = definition_builder(**{self.system_tool_enabled_kwarg: enabled})
            owned_tools: list[Any] = []
            for tool in tools:
                implementation = tool.implementation.model_copy(
                    update={
                        "config": {
                            **tool.implementation.config,
                            OPTIONAL_MODULE_TOOL_OWNER_CONFIG_KEY: self.key,
                        }
                    }
                )
                owned_tools.append(tool.model_copy(update={"implementation": implementation}))
            return owned_tools

        def ids(enabled: bool) -> list[str]:
            return id_builder(**{self.system_tool_enabled_kwarg: enabled})

        return definitions, ids

    def load_persistence_manifest(self) -> dict[str, Any] | None:
        if not self.persistence_manifest_ref:
            return None
        manifest_builder = _load_ref(self.persistence_manifest_ref)
        manifest = manifest_builder()
        if not isinstance(manifest, dict):
            raise TypeError(f"Optional module '{self.key}' persistence manifest must be a dict")
        return manifest

    def load_domain_manifest(self) -> dict[str, Any] | None:
        if not self.domain_manifest_ref:
            return None
        manifest_builder = _load_ref(self.domain_manifest_ref)
        manifest = manifest_builder()
        if not isinstance(manifest, dict):
            raise TypeError(f"Optional module '{self.key}' domain manifest must be a dict")
        return manifest

    def load_observability_manifest(self) -> dict[str, Any] | None:
        if not self.observability_manifest_ref:
            return None
        manifest_builder = _load_ref(self.observability_manifest_ref)
        manifest = manifest_builder()
        if not isinstance(manifest, dict):
            raise TypeError(f"Optional module '{self.key}' observability manifest must be a dict")
        return manifest

    def load_graph_manifest(self) -> dict[str, Any] | None:
        if not self.graph_manifest_ref:
            return None
        manifest_builder = _load_ref(self.graph_manifest_ref)
        manifest = manifest_builder()
        if not isinstance(manifest, dict):
            raise TypeError(f"Optional module '{self.key}' graph manifest must be a dict")
        return manifest

    def capabilities(self, *, visible_tool_names: Callable[[list[str]], list[str]]) -> dict[str, Any]:
        available = self.available()
        payload = {
            "available": available,
            "status": "available" if available else "disabled",
            "reason": None if available else self.unavailable_reason(),
            "displayName": self.display_name,
            "canonicalNamespace": self.canonical_namespace,
            "routePrefix": self.route_prefix,
            "readScopes": list(self.read_scopes),
            "writeScopes": list(self.write_scopes),
            "frontend": self.frontend,
            "moduleLoading": {
                "entryPointGroup": OPTIONAL_MODULE_ENTRY_POINT_GROUP,
                "configRefEnv": "AGENCY_OPTIONAL_MODULE_SPEC_REFS",
                "expectedModulesEnv": "AGENCY_EXPECTED_OPTIONAL_MODULES",
                "settingName": self.setting_name,
            },
            "hiddenWhenUnavailable": {
                "routePrefixes": list(self.route_prefixes),
                "toolNames": list(self.tool_names),
            },
            "tools": {
                "preferred": visible_tool_names(list(self.preferred_tool_names)),
                "vendorSpecific": visible_tool_names(list(self.vendor_specific_tool_names)),
            },
            "notes": list(self.notes),
        }
        if self.read_only_tool_names or self.mutating_tool_names:
            payload["tools"]["readOnly"] = visible_tool_names(list(self.read_only_tool_names))
            payload["tools"]["mutating"] = visible_tool_names(list(self.mutating_tool_names))
        if self.package_name or self.package_version or self.install_hint:
            payload["package"] = {
                "name": self.package_name,
                "version": self.package_version,
                "installHint": self.install_hint,
            }
        if self.extra_capabilities:
            payload.update(self.extra_capabilities)
        return payload


def _load_ref(ref: str) -> Any:
    if ":" not in ref:
        raise RuntimeError(f"Optional module spec ref '{ref}' must use 'module:attribute' format")
    module_name, function_name = ref.split(":", 1)
    return getattr(import_module(module_name), function_name)


def _module_ref_importable(ref: str) -> bool:
    module_name = ref.split(":", 1)[0]
    try:
        return find_spec(module_name) is not None
    except (AttributeError, ImportError, ValueError):
        return False


def _parse_bool_env(value: str, *, env_name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{env_name} must be a boolean value")


OPTIONAL_MODULE_SPECS: tuple[OptionalModuleSpec, ...] = ()


def _configured_optional_module_specs() -> tuple[OptionalModuleSpec, ...]:
    specs: list[OptionalModuleSpec] = []
    for ref in dict.fromkeys(get_settings().parsed_agency_optional_module_spec_refs):
        loaded = _load_ref(ref)
        value = loaded() if callable(loaded) else loaded
        specs.extend(_coerce_optional_module_specs(value, ref=ref))
    return tuple(specs)


def _entry_point_optional_module_specs() -> tuple[OptionalModuleSpec, ...]:
    if not get_settings().agency_optional_module_entry_points_enabled:
        return ()
    specs: list[OptionalModuleSpec] = []
    for entry_point in entry_points(group=OPTIONAL_MODULE_ENTRY_POINT_GROUP):
        value = entry_point.load()
        loaded = value() if callable(value) else value
        specs.extend(
            _coerce_optional_module_specs(loaded, ref=f"{OPTIONAL_MODULE_ENTRY_POINT_GROUP}:{entry_point.name}"))
    return tuple(specs)


def _coerce_optional_module_specs(value: Any, *, ref: str) -> tuple[OptionalModuleSpec, ...]:
    if isinstance(value, OptionalModuleSpec):
        return (value,)
    if isinstance(value, (list, tuple)):
        specs: list[OptionalModuleSpec] = []
        for item in value:
            if not isinstance(item, OptionalModuleSpec):
                raise TypeError(f"Optional module spec ref '{ref}' returned a non-OptionalModuleSpec item")
            specs.append(item)
        return tuple(specs)
    raise TypeError(f"Optional module spec ref '{ref}' must return OptionalModuleSpec or a list/tuple of specs")


def _validate_unique_module_specs(specs: tuple[OptionalModuleSpec, ...]) -> None:
    seen: dict[str, str] = {}
    for spec in specs:
        existing = seen.get(spec.key)
        if existing is not None:
            raise RuntimeError(
                f"Duplicate optional module key '{spec.key}' from {existing} and {spec.canonical_namespace}")
        seen[spec.key] = spec.canonical_namespace


def _validate_tool_metadata(spec: OptionalModuleSpec) -> None:
    overlapping_tool_names = sorted(set(spec.preferred_tool_names) & set(spec.vendor_specific_tool_names))
    if overlapping_tool_names:
        raise RuntimeError(
            f"Optional module '{spec.key}' cannot mark the same tools as both preferred and vendor-specific: "
            + ", ".join(overlapping_tool_names)
        )


def optional_module_specs() -> tuple[OptionalModuleSpec, ...]:
    builtin_keys = set(get_settings().parsed_agency_builtin_optional_modules)
    known_builtin_keys = {spec.key for spec in OPTIONAL_MODULE_SPECS}
    unknown_builtin_keys = sorted(builtin_keys - known_builtin_keys)
    if unknown_builtin_keys:
        raise RuntimeError(
            "AGENCY_BUILTIN_OPTIONAL_MODULES contains unknown module keys: "
            + ", ".join(unknown_builtin_keys)
        )
    builtin_specs = tuple(spec for spec in OPTIONAL_MODULE_SPECS if spec.key in builtin_keys)
    # Core does not ship add-on pack specs. Installed entry-point packs and
    # explicit config refs are the optional module sources here.
    configured_specs = _configured_optional_module_specs()
    configured_keys = {spec.key for spec in configured_specs}
    entry_point_specs = tuple(
        spec
        for spec in _entry_point_optional_module_specs()
        if spec.key not in builtin_keys and spec.key not in configured_keys
    )
    specs = (*builtin_specs, *entry_point_specs, *configured_specs)
    _validate_unique_module_specs(specs)
    for spec in specs:
        # Preferred tools are the generic path agents should reach for first,
        # while vendor-specific tools are explicit escape hatches. Allowing the
        # same tool in both buckets collapses that distinction for clients.
        _validate_tool_metadata(spec)
    return specs


def optional_module_available(module_key: str) -> bool:
    return any(spec.key == module_key and spec.available() for spec in optional_module_specs())


def validate_expected_optional_modules(expected_modules: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    specs = {spec.key: spec for spec in optional_module_specs()}
    errors: list[str] = []
    for module_key in dict.fromkeys(module for module in expected_modules if module):
        spec = specs.get(module_key)
        if spec is None:
            errors.append(f"expected optional module '{module_key}' is not registered")
            continue
        if not spec.available():
            errors.append(
                f"expected optional module '{module_key}' is registered but unavailable: {spec.unavailable_reason()}"
            )
    return tuple(errors)


def optional_module_capabilities(*, visible_tool_names: Callable[[list[str]], list[str]]) -> dict[str, dict[str, Any]]:
    return {spec.key: spec.capabilities(visible_tool_names=visible_tool_names) for spec in optional_module_specs()}


def optional_module_route_factories() -> list[RouteFactory]:
    factories: list[RouteFactory] = []
    for spec in optional_module_specs():
        if not spec.available():
            continue
        factories.extend(spec.load_route_factories())
    return factories


def optional_module_system_tool_family_builders() -> dict[
    str,
    tuple[Callable[[bool], list[Any]], Callable[[bool], list[str]]],
]:
    builders: dict[str, tuple[Callable[[bool], list[Any]], Callable[[bool], list[str]]]] = {}
    for spec in optional_module_specs():
        if not spec.available():
            continue
        loaded = spec.load_system_tool_builders()
        if loaded is not None and spec.system_tool_family_key:
            builders[spec.system_tool_family_key] = loaded
    return builders


def optional_module_raw_system_tool_definition_builder(module_key: str) -> Any | None:
    for spec in optional_module_specs():
        if spec.key == module_key and spec.available() and spec.system_tool_definition_builder_ref:
            return _load_ref(spec.system_tool_definition_builder_ref)
    return None


def optional_module_raw_system_tool_id_builder(module_key: str) -> Any | None:
    for spec in optional_module_specs():
        if spec.key == module_key and spec.available() and spec.system_tool_id_builder_ref:
            return _load_ref(spec.system_tool_id_builder_ref)
    return None


def optional_module_agent_tool_profile_builder(module_key: str) -> Any | None:
    for spec in optional_module_specs():
        if spec.key == module_key and spec.available() and spec.agent_tool_profile_builder_ref:
            return _load_ref(spec.agent_tool_profile_builder_ref)
    return None


def optional_module_persistence_manifests() -> dict[str, dict[str, Any]]:
    manifests: dict[str, dict[str, Any]] = {}
    for spec in optional_module_specs():
        if not spec.available():
            continue
        manifest = spec.load_persistence_manifest()
        if manifest is not None:
            _validate_persistence_manifest(spec.key, manifest)
            manifests[spec.key] = manifest
    return manifests


def _validate_ref(value: str, *, field_name: str, module_key: str) -> None:
    if ":" not in value:
        raise RuntimeError(f"Optional module '{module_key}' {field_name} ref '{value}' must use 'module:attribute'")


def _validate_persistence_manifest(module_key: str, manifest: dict[str, Any]) -> None:
    declared_module = manifest.get("module")
    if declared_module != module_key:
        raise RuntimeError(
            f"Optional module '{module_key}' persistence manifest must declare module='{module_key}'"
        )

    revisions = tuple(str(revision) for revision in manifest.get("alembic_revisions", ()) if revision)
    paths = tuple(str(path) for path in manifest.get("alembic_version_paths", ()) if path)
    if len(revisions) != len(paths):
        raise RuntimeError(
            f"Optional module '{module_key}' persistence manifest must pair each Alembic revision with one path"
        )
    for ref in manifest.get("orm_model_refs", ()):
        _validate_ref(str(ref), field_name="orm_model_refs", module_key=module_key)
    dependencies = manifest.get("migration_dependencies", {})
    if dependencies and not isinstance(dependencies, dict):
        raise RuntimeError(
            f"Optional module '{module_key}' persistence manifest migration_dependencies must be a dict"
        )
    migration_source = str(manifest.get("migration_source", "core"))
    if migration_source not in {"core", "package"}:
        raise RuntimeError(
            f"Optional module '{module_key}' persistence manifest migration_source must be 'core' or 'package'"
        )
    removal_policy = str(manifest.get("removal_policy", "manual"))
    if removal_policy not in {"manual", "drop_owned_tables", "preserve_data"}:
        raise RuntimeError(
            f"Optional module '{module_key}' persistence manifest removal_policy must be "
            "'manual', 'drop_owned_tables', or 'preserve_data'"
        )
    removal_notes = manifest.get("removal_notes", ())
    if isinstance(removal_notes, str) or not isinstance(removal_notes, (list, tuple)):
        raise RuntimeError(
            f"Optional module '{module_key}' persistence manifest removal_notes must be a list or tuple"
        )


def optional_module_domain_manifests() -> dict[str, dict[str, Any]]:
    manifests: dict[str, dict[str, Any]] = {}
    for spec in optional_module_specs():
        if not spec.available():
            continue
        manifest = spec.load_domain_manifest()
        if manifest is not None:
            manifests[spec.key] = manifest
    return manifests


def optional_module_observability_manifests() -> dict[str, dict[str, Any]]:
    manifests: dict[str, dict[str, Any]] = {}
    for spec in optional_module_specs():
        if not spec.available():
            continue
        manifest = spec.load_observability_manifest()
        if manifest is not None:
            manifests[spec.key] = manifest
    return manifests


def optional_module_observability_hook_refs(module_key: str) -> dict[str, str]:
    manifest = optional_module_observability_manifests().get(module_key, {})
    refs = manifest.get("hook_refs", {})
    if not isinstance(refs, dict):
        return {}
    return {str(name): str(ref) for name, ref in refs.items() if name and ref}


def optional_module_graph_manifests() -> dict[str, dict[str, Any]]:
    manifests: dict[str, dict[str, Any]] = {}
    for spec in optional_module_specs():
        if not spec.available():
            continue
        manifest = spec.load_graph_manifest()
        if manifest is not None:
            manifests[spec.key] = manifest
    return manifests


def optional_module_graph_delta_builder_refs() -> tuple[str, ...]:
    refs: list[str] = []
    for manifest in optional_module_graph_manifests().values():
        refs.extend(str(ref) for ref in manifest.get("delta_builder_refs", ()) if ref)
    return tuple(dict.fromkeys(refs))


def optional_module_neo4j_projection_handler_map_refs() -> tuple[str, ...]:
    refs: list[str] = []
    for manifest in optional_module_graph_manifests().values():
        refs.extend(str(ref) for ref in manifest.get("neo4j_handler_map_refs", ()) if ref)
    return tuple(dict.fromkeys(refs))


def append_optional_module_graph_deltas(*args, **kwargs) -> None:
    for ref in optional_module_graph_delta_builder_refs():
        _load_ref(ref)(*args, **kwargs)


def optional_module_neo4j_projection_handlers() -> dict[str, Any]:
    handlers: dict[str, Any] = {}
    for ref in optional_module_neo4j_projection_handler_map_refs():
        loaded = _load_ref(ref)()
        if not isinstance(loaded, dict):
            raise TypeError(f"Optional Neo4j projection handler map '{ref}' must return a dict")
        handlers.update(loaded)
    return handlers


def publish_optional_module_observability(module_key: str, hook_name: str, *args, **kwargs) -> None:
    hook_ref = optional_module_observability_hook_refs(module_key).get(hook_name)
    if hook_ref is None:
        return None
    _load_ref(hook_ref)(*args, **kwargs)
    return None


def optional_module_domain_model_refs() -> tuple[str, ...]:
    refs: list[str] = []
    for manifest in optional_module_domain_manifests().values():
        refs.extend(str(ref) for ref in manifest.get("model_refs", ()) if ref)
    return tuple(dict.fromkeys(refs))


def optional_module_orm_model_refs() -> tuple[str, ...]:
    refs: list[str] = []
    for manifest in optional_module_persistence_manifests().values():
        refs.extend(str(ref) for ref in manifest.get("orm_model_refs", ()) if ref)
    return tuple(dict.fromkeys(refs))


def optional_module_migration_refs() -> tuple[str, ...]:
    refs: list[str] = []
    for manifest in optional_module_persistence_manifests().values():
        refs.extend(str(ref) for ref in manifest.get("alembic_version_paths", ()) if ref)
    return tuple(dict.fromkeys(refs))


def _migration_version_location(path: str) -> str:
    migration_path = Path(path)
    parent = migration_path.parent
    return str(parent if str(parent) else Path("."))


def optional_module_persistence_plans() -> dict[str, OptionalModulePersistencePlan]:
    plans: dict[str, OptionalModulePersistencePlan] = {}
    for module_key, manifest in optional_module_persistence_manifests().items():
        revisions = tuple(str(revision) for revision in manifest.get("alembic_revisions", ()) if revision)
        paths = tuple(str(path) for path in manifest.get("alembic_version_paths", ()) if path)
        migration_dependencies = manifest.get("migration_dependencies", {})
        after_modules = tuple(str(name) for name in manifest.get("after_modules", ()) if name)
        migrations = tuple(
            OptionalModuleMigration(
                module_key=module_key,
                revision=revision,
                path=path,
                version_location=_migration_version_location(path),
                depends_on_revisions=tuple(
                    str(item) for item in migration_dependencies.get(revision, ()) if item
                )
                if isinstance(migration_dependencies, dict)
                else (),
                after_modules=after_modules,
            )
            for revision, path in zip(revisions, paths)
        )
        plans[module_key] = OptionalModulePersistencePlan(
            module_key=module_key,
            orm_model_refs=tuple(str(ref) for ref in manifest.get("orm_model_refs", ()) if ref),
            migrations=migrations,
            tables=tuple(str(table) for table in manifest.get("tables", ()) if table),
            managed_by=str(manifest.get("managed_by", "alembic")),
            migration_source=str(manifest.get("migration_source", "core")),
            removal_policy=str(manifest.get("removal_policy", "manual")),
            removal_notes=tuple(str(note) for note in manifest.get("removal_notes", ()) if note),
        )
    return plans


def validate_optional_module_migration_ordering(
        plans: dict[str, OptionalModulePersistencePlan] | None = None,
) -> tuple[str, ...]:
    plans = plans if plans is not None else optional_module_persistence_plans()
    errors: list[str] = []
    revision_to_module: dict[str, str] = {}
    duplicate_revisions: set[str] = set()

    for plan in plans.values():
        for migration in plan.migrations:
            existing = revision_to_module.get(migration.revision)
            if existing is not None:
                duplicate_revisions.add(migration.revision)
                errors.append(
                    f"duplicate migration revision '{migration.revision}' declared by {existing} and {plan.module_key}"
                )
            revision_to_module[migration.revision] = plan.module_key

    for plan in plans.values():
        for migration in plan.migrations:
            for dependency in migration.depends_on_revisions:
                if dependency not in revision_to_module:
                    errors.append(
                        f"{plan.module_key}:{migration.revision} depends on unknown optional revision '{dependency}'"
                    )
            for module_key in migration.after_modules:
                if module_key not in plans:
                    errors.append(f"{plan.module_key}:{migration.revision} orders after unknown module '{module_key}'")

    dependency_graph: dict[str, set[str]] = {
        migration.revision: set(migration.depends_on_revisions)
        for plan in plans.values()
        for migration in plan.migrations
        if migration.revision not in duplicate_revisions
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(revision: str, path: tuple[str, ...]) -> None:
        if revision in visited:
            return
        if revision in visiting:
            cycle = " -> ".join((*path, revision))
            errors.append(f"migration dependency cycle detected: {cycle}")
            return
        visiting.add(revision)
        for dependency in dependency_graph.get(revision, set()):
            if dependency in dependency_graph:
                visit(dependency, (*path, revision))
        visiting.remove(revision)
        visited.add(revision)

    for revision in dependency_graph:
        visit(revision, ())

    module_graph: dict[str, set[str]] = {
        module_key: {
            dependency
            for migration in plan.migrations
            for dependency in migration.after_modules
            if dependency in plans
        }
        for module_key, plan in plans.items()
    }
    visiting_modules: set[str] = set()
    visited_modules: set[str] = set()

    def visit_module(module_key: str, path: tuple[str, ...]) -> None:
        if module_key in visited_modules:
            return
        if module_key in visiting_modules:
            cycle = " -> ".join((*path, module_key))
            errors.append(f"module migration ordering cycle detected: {cycle}")
            return
        visiting_modules.add(module_key)
        for dependency in module_graph.get(module_key, set()):
            visit_module(dependency, (*path, module_key))
        visiting_modules.remove(module_key)
        visited_modules.add(module_key)

    for module_key in module_graph:
        visit_module(module_key, ())

    return tuple(dict.fromkeys(errors))


def optional_module_alembic_version_locations(*, include_core: bool = True) -> tuple[str, ...]:
    locations: list[str] = ["alembic/versions"] if include_core else []
    for plan in optional_module_persistence_plans().values():
        locations.extend(migration.version_location for migration in plan.migrations)
    return tuple(dict.fromkeys(locations))


def load_optional_module_orm_models() -> None:
    # Alembic autogenerate needs ORM classes imported so their tables are registered
    # on Base.metadata. The refs stay module-owned, which is the extraction seam.
    for ref in optional_module_orm_model_refs():
        _load_ref(ref)


def load_optional_module_domain_models() -> None:
    # Domain models stay compatible with app.domain today, but optional modules
    # own the refs so a later package split has a single discovery seam.
    for ref in optional_module_domain_model_refs():
        _load_ref(ref)


def optional_module_runtime_tool_names(module_key: str | None = None) -> set[str]:
    names: set[str] = set()
    for spec in optional_module_specs():
        if module_key is not None and spec.key != module_key:
            continue
        if not spec.available():
            continue
        names.update(spec.runtime_tool_names)
    return names


def optional_module_key_for_runtime_tool(tool_name: str) -> str | None:
    for spec in optional_module_specs():
        if spec.available() and tool_name in spec.runtime_tool_names:
            return spec.key
    return None


def optional_module_runtime_tool_handler_class(module_key: str) -> Any | None:
    for spec in optional_module_specs():
        if spec.key == module_key and spec.available() and spec.runtime_tool_handler_ref:
            return _load_ref(spec.runtime_tool_handler_ref)
    return None


def optional_module_memory_service_class(module_key: str) -> Any | None:
    for spec in optional_module_specs():
        if spec.key == module_key and spec.available() and spec.memory_service_ref:
            return _load_ref(spec.memory_service_ref)
    return None


def optional_module_service_ref(module_key: str, service_name: str) -> Any | None:
    for spec in optional_module_specs():
        if spec.key != module_key or not spec.available():
            continue
        ref = (spec.service_refs or {}).get(service_name)
        if ref:
            return _load_ref(ref)
    return None


def optional_module_exception_classes(module_key: str) -> dict[str, type[Exception]]:
    for spec in optional_module_specs():
        if spec.key != module_key or not spec.available():
            continue
        classes: dict[str, type[Exception]] = {}
        for name, ref in (spec.exception_refs or {}).items():
            loaded = _load_ref(ref)
            if not isinstance(loaded, type) or not issubclass(loaded, Exception):
                raise TypeError(f"Optional module '{module_key}' exception ref '{ref}' must load an Exception class")
            classes[name] = loaded
        return classes
    return {}


def optional_module_adapter_factory(module_key: str) -> Any | None:
    for spec in optional_module_specs():
        if spec.key == module_key and spec.available() and spec.adapter_factory_ref:
            return _load_ref(spec.adapter_factory_ref)
    return None


def optional_module_context_repository_factory(module_key: str) -> Any | None:
    for spec in optional_module_specs():
        if spec.key == module_key and spec.available() and spec.context_repository_factory_ref:
            return _load_ref(spec.context_repository_factory_ref)
    return None


def optional_module_context_resolver_class(module_key: str) -> Any | None:
    for spec in optional_module_specs():
        if spec.key == module_key and spec.available() and spec.context_resolver_class_ref:
            return _load_ref(spec.context_resolver_class_ref)
    return None


def optional_module_tool_names() -> dict[str, set[str]]:
    return {spec.key: set(spec.tool_names) for spec in optional_module_specs()}


def hidden_tool_names_for_disabled_modules() -> set[str]:
    hidden: set[str] = set()
    for spec in optional_module_specs():
        if not spec.available():
            hidden.update(spec.tool_names)
    return hidden


__all__ = [
    "OptionalModuleSpec",
    "OptionalModuleMigration",
    "OptionalModulePersistencePlan",
    "OPTIONAL_MODULE_ENTRY_POINT_GROUP",
    "OPTIONAL_MODULE_TOOL_OWNER_CONFIG_KEY",
    "hidden_tool_names_for_disabled_modules",
    "optional_module_available",
    "load_optional_module_domain_models",
    "optional_module_capabilities",
    "load_optional_module_orm_models",
    "optional_module_domain_manifests",
    "optional_module_domain_model_refs",
    "append_optional_module_graph_deltas",
    "optional_module_migration_refs",
    "optional_module_alembic_version_locations",
    "optional_module_persistence_plans",
    "validate_optional_module_migration_ordering",
    "optional_module_graph_delta_builder_refs",
    "optional_module_graph_manifests",
    "optional_module_neo4j_projection_handler_map_refs",
    "optional_module_neo4j_projection_handlers",
    "optional_module_observability_hook_refs",
    "optional_module_observability_manifests",
    "optional_module_orm_model_refs",
    "optional_module_persistence_manifests",
    "optional_module_agent_tool_profile_builder",
    "optional_module_raw_system_tool_definition_builder",
    "optional_module_raw_system_tool_id_builder",
    "publish_optional_module_observability",
    "optional_module_key_for_runtime_tool",
    "optional_module_runtime_tool_names",
    "optional_module_runtime_tool_handler_class",
    "optional_module_memory_service_class",
    "optional_module_service_ref",
    "optional_module_exception_classes",
    "optional_module_adapter_factory",
    "optional_module_context_repository_factory",
    "optional_module_context_resolver_class",
    "optional_module_route_factories",
    "optional_module_specs",
    "optional_module_system_tool_family_builders",
    "optional_module_tool_names",
    "validate_expected_optional_modules",
]
