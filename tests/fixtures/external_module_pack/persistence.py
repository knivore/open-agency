from __future__ import annotations


class ExternalExampleORM:
    __tablename__ = "external_example_items"


def external_example_persistence_manifest() -> dict[str, object]:
    return {
        "module": "external_example_pack",
        "orm_model_refs": (
            "tests.fixtures.external_module_pack.persistence:ExternalExampleORM",
        ),
        "alembic_revisions": (
            "20260701_0001_external_example",
        ),
        "alembic_version_paths": (
            "tests/fixtures/external_module_pack/migrations/20260701_0001_external_example.py",
        ),
        "migration_source": "package",
        "removal_policy": "preserve_data",
        "removal_notes": (
            "External example data is preserved unless an operator explicitly drops owned tables.",
        ),
        "tables": (
            "external_example_items",
        ),
    }

