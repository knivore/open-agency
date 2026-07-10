# External Module Pack Fixture

This fixture models the shape of a future installable Agency module pack.

Real packs should expose `manifest:module_spec` through either:

- `AGENCY_OPTIONAL_MODULE_SPEC_REFS=package.manifest:module_spec`
- `[project.entry-points."agency.module_packs"]` in `pyproject.toml`

The package owns its routes, tools, runtime handler, manifests, hooks, and
migration refs. Agency core discovers those through `app.modules.registry`
without importing implementation modules directly.

