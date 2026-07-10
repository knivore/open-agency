#!/usr/bin/env python3
"""Validate optional module persistence manifests without applying migrations."""

from __future__ import annotations

import argparse
import json
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

try:
    from scripts._bootstrap import bootstrap_repo
except ModuleNotFoundError:
    from _bootstrap import bootstrap_repo

bootstrap_repo(__file__, reexec=__name__ == "__main__")

from app.core.config import get_settings
from app.modules.registry import (
    OptionalModulePersistencePlan,
    optional_module_alembic_version_locations,
    optional_module_persistence_plans,
    validate_optional_module_migration_ordering,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ref_exists(ref: str) -> bool:
    module_name, attr_name = ref.split(":", 1)
    return hasattr(import_module(module_name), attr_name)


def _plan_payload(plan: OptionalModulePersistencePlan, *, repo_root: Path, check_paths: bool) -> dict[str, Any]:
    orm_refs = []
    for ref in plan.orm_model_refs:
        orm_refs.append(
            {
                "ref": ref,
                "importable": _ref_exists(ref),
            }
        )

    migrations = []
    for migration in plan.migrations:
        path = repo_root / migration.path
        migrations.append(
            {
                "revision": migration.revision,
                "path": migration.path,
                "versionLocation": migration.version_location,
                "dependsOnRevisions": list(migration.depends_on_revisions),
                "afterModules": list(migration.after_modules),
                "pathExists": path.exists() if check_paths else None,
            }
        )

    return {
        "module": plan.module_key,
        "managedBy": plan.managed_by,
        "migrationSource": plan.migration_source,
        "removalPolicy": plan.removal_policy,
        "removalNotes": list(plan.removal_notes),
        "tables": list(plan.tables),
        "ormModelRefs": orm_refs,
        "migrations": migrations,
    }


def build_report(
    *,
    check_paths: bool = False,
    expected_modules: tuple[str, ...] | None = None,
) -> tuple[dict[str, Any], int]:
    repo_root = _repo_root()
    plans = optional_module_persistence_plans()
    configured_expected_modules = tuple(get_settings().parsed_agency_expected_optional_modules)
    requested_expected_modules = expected_modules if expected_modules is not None else ()
    expected = tuple(
        dict.fromkeys(module for module in (*configured_expected_modules, *requested_expected_modules) if module)
    )
    payload = {
        "ok": True,
        "moduleCount": len(plans),
        "expectedModules": list(expected),
        "alembicVersionLocations": list(optional_module_alembic_version_locations()),
        "modules": [],
        "errors": [],
    }

    for module_key in expected:
        if module_key not in plans:
            payload["errors"].append(
                f"expected optional module '{module_key}' is not registered with a persistence plan"
            )

    for plan in plans.values():
        module_payload = _plan_payload(plan, repo_root=repo_root, check_paths=check_paths)
        payload["modules"].append(module_payload)
        for ref in module_payload["ormModelRefs"]:
            if not ref["importable"]:
                payload["errors"].append(f"{module_payload['module']}: ORM ref is not importable: {ref['ref']}")
        if check_paths:
            for migration in module_payload["migrations"]:
                if not migration["pathExists"]:
                    payload["errors"].append(
                        f"{module_payload['module']}: migration path does not exist: {migration['path']}"
                    )
    payload["errors"].extend(validate_optional_module_migration_ordering(plans))

    payload["ok"] = not payload["errors"]
    return payload, 0 if payload["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate optional module persistence manifests without applying migrations."
    )
    parser.add_argument(
        "--check-paths",
        action="store_true",
        help="Verify declared Alembic migration paths exist on disk.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    parser.add_argument(
        "--expect-module",
        action="append",
        default=[],
        metavar="MODULE_KEY",
        help=(
            "Require a module persistence plan to be registered. Repeat for deployments "
            "that expect specific optional packs to be installed. Values are combined "
            "with AGENCY_EXPECTED_OPTIONAL_MODULES."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report, exit_code = build_report(
        check_paths=args.check_paths,
        expected_modules=tuple(args.expect_module),
    )
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
