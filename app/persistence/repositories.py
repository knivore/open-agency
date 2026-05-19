"""Deprecated compatibility re-export.

Prefer importing catalog repositories from ``app.db.repositories``.
"""

from app.db.repositories.catalog import (
    BaseCatalogRepository,
    BUILTIN_RUNTIME_ADAPTERS,
    InMemoryCatalogRepository,
    InMemoryModelProfileCatalogRepository,
    InMemoryWorkflowCatalogRepository,
    ModelProfileCatalogRepository,
    MongoCatalogRepository,
    WorkflowCatalogRepository,
    ensure_builtin_runtime_adapters,
)

__all__ = [
    "BaseCatalogRepository",
    "BUILTIN_RUNTIME_ADAPTERS",
    "InMemoryCatalogRepository",
    "InMemoryModelProfileCatalogRepository",
    "InMemoryWorkflowCatalogRepository",
    "ModelProfileCatalogRepository",
    "MongoCatalogRepository",
    "WorkflowCatalogRepository",
    "ensure_builtin_runtime_adapters",
]
