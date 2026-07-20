from __future__ import annotations

from dataclasses import dataclass
from motor.motor_asyncio import AsyncIOMotorDatabase
from typing import Any, Dict, Generic, List, Optional, Type, TypeVar

from app.core.time import utc_now
from app.db.mongo import get_mongodb_db_name, mongo_db_connect
from app.domain import (
    ModelProfileDefinition,
    RuntimeAdapterDefinition,
    RuntimeAdapterType,
    RuntimeRevision,
    RuntimeRevisionStatus,
    WorkflowDefinition,
)

T = TypeVar("T")


class BaseCatalogRepository(Generic[T]):
    model_cls: Type[T]

    async def create(self, item: T) -> T:
        raise NotImplementedError

    async def save(self, item: T) -> T:
        raise NotImplementedError

    async def list(self, *, include_deleted: bool = False) -> List[T]:
        raise NotImplementedError

    async def get(self, item_id: str, *, include_deleted: bool = False) -> Optional[T]:
        raise NotImplementedError

    async def update(self, item_id: str, patch: Dict[str, Any]) -> Optional[T]:
        raise NotImplementedError

    async def soft_delete(self, item_id: str) -> bool:
        raise NotImplementedError

    async def restore(self, item_id: str) -> bool:
        raise NotImplementedError


class InMemoryCatalogRepository(BaseCatalogRepository[T]):
    def __init__(self, model_cls: Type[T]):
        self.model_cls = model_cls
        self._items: Dict[str, Dict[str, Any]] = {}

    def _serialize(self, item: T) -> Dict[str, Any]:
        payload = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        # Preserve internal mirror fields that are intentionally excluded from normal API dumps.
        if hasattr(item, "runtime_secret_encrypted") and "runtime_secret_encrypted" not in payload:
            payload["runtime_secret_encrypted"] = getattr(item, "runtime_secret_encrypted")
        return payload

    def _deserialize(self, payload: Dict[str, Any]) -> T:
        clean = {key: value for key, value in payload.items() if not key.startswith("_")}
        return self.model_cls.model_validate(clean)

    async def create(self, item: T) -> T:
        payload = self._serialize(item)
        payload["_deleted_at"] = None
        payload["_updated_at"] = utc_now().isoformat()
        self._items[payload["id"]] = payload
        return self._deserialize(payload)

    async def save(self, item: T) -> T:
        payload = self._serialize(item)
        existing = self._items.get(payload["id"], {})
        payload["_deleted_at"] = existing.get("_deleted_at")
        payload["_updated_at"] = utc_now().isoformat()
        self._items[payload["id"]] = payload
        return self._deserialize(payload)

    async def list(self, *, include_deleted: bool = False) -> List[T]:
        items = []
        for payload in self._items.values():
            if not include_deleted and payload.get("_deleted_at"):
                continue
            items.append(self._deserialize(payload))
        return items

    async def get(self, item_id: str, *, include_deleted: bool = False) -> Optional[T]:
        payload = self._items.get(item_id)
        if not payload:
            return None
        if not include_deleted and payload.get("_deleted_at"):
            return None
        return self._deserialize(payload)

    async def update(self, item_id: str, patch: Dict[str, Any]) -> Optional[T]:
        existing = self._items.get(item_id)
        if not existing or existing.get("_deleted_at"):
            return None
        merged = {key: value for key, value in existing.items() if not key.startswith("_")}
        merged.update(patch)
        merged["id"] = item_id
        model = self.model_cls.model_validate(merged)
        return await self.save(model)

    async def soft_delete(self, item_id: str) -> bool:
        existing = self._items.get(item_id)
        if not existing or existing.get("_deleted_at"):
            return False
        existing["_deleted_at"] = utc_now().isoformat()
        existing["_updated_at"] = utc_now().isoformat()
        return True

    async def restore(self, item_id: str) -> bool:
        existing = self._items.get(item_id)
        if not existing:
            return False
        existing["_deleted_at"] = None
        existing["_updated_at"] = utc_now().isoformat()
        return True


class InMemoryRuntimeRevisionRepository(InMemoryCatalogRepository[RuntimeRevision]):
    """In-memory runtime revision store matching the SQL repository contract used by tests."""

    def __init__(self):
        super().__init__(RuntimeRevision)

    async def list(self, *, include_deleted: bool = False) -> List[RuntimeRevision]:
        items = await super().list(include_deleted=True)
        if not include_deleted:
            items = [item for item in items if item.build_status != RuntimeRevisionStatus.INVALIDATED]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    async def get(self, item_id: str, *, include_deleted: bool = False) -> Optional[RuntimeRevision]:
        item = await super().get(item_id, include_deleted=True)
        if item is None:
            return None
        if not include_deleted and item.build_status == RuntimeRevisionStatus.INVALIDATED:
            return None
        return item

    async def get_by_fingerprint(self, fingerprint: str) -> RuntimeRevision | None:
        for item in await super().list(include_deleted=True):
            if item.fingerprint == fingerprint:
                return item
        return None

    async def get_latest_ready(self) -> RuntimeRevision | None:
        ready = [
            item
            for item in await super().list(include_deleted=True)
            if item.build_status == RuntimeRevisionStatus.READY
        ]
        return max(ready, key=lambda item: item.created_at, default=None)

    async def invalidate_revision(
            self,
            item_id: str,
            *,
            reason: str | None = None,
    ) -> RuntimeRevision | None:
        current = await self.get(item_id, include_deleted=True)
        if current is None:
            return None
        return await self.update(
            item_id,
            {
                "build_status": RuntimeRevisionStatus.INVALIDATED.value,
                "invalidated_at": utc_now().isoformat(),
                "invalidation_reason": reason,
            },
        )


class MongoCatalogRepository(BaseCatalogRepository[T]):
    def __init__(self, model_cls: Type[T], collection_name: str, db: Optional[AsyncIOMotorDatabase] = None):
        self.model_cls = model_cls
        if db is None:
            client = mongo_db_connect()
            db = client.get_database(get_mongodb_db_name())
        self.collection = db[collection_name]

    def _serialize(self, item: T) -> Dict[str, Any]:
        return item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)

    def _deserialize(self, payload: Dict[str, Any]) -> T:
        payload.pop("_id", None)
        payload.pop("_deleted_at", None)
        payload.pop("_updated_at", None)
        return self.model_cls.model_validate(payload)

    async def create(self, item: T) -> T:
        payload = self._serialize(item)
        payload["_deleted_at"] = None
        payload["_updated_at"] = utc_now().isoformat()
        await self.collection.insert_one(payload)
        return self._deserialize(payload)

    async def save(self, item: T) -> T:
        payload = self._serialize(item)
        payload["_updated_at"] = utc_now().isoformat()
        existing = await self.collection.find_one({"id": payload["id"]})
        payload["_deleted_at"] = existing.get("_deleted_at") if existing else None
        await self.collection.update_one({"id": payload["id"]}, {"$set": payload}, upsert=True)
        return self._deserialize(payload)

    async def list(self, *, include_deleted: bool = False) -> List[T]:
        query: Dict[str, Any] = {}
        if not include_deleted:
            query["_deleted_at"] = None
        cursor = self.collection.find(query)
        items: List[T] = []
        async for payload in cursor:
            items.append(self._deserialize(payload))
        return items

    async def get(self, item_id: str, *, include_deleted: bool = False) -> Optional[T]:
        query: Dict[str, Any] = {"id": item_id}
        if not include_deleted:
            query["_deleted_at"] = None
        payload = await self.collection.find_one(query)
        if payload is None:
            return None
        return self._deserialize(payload)

    async def update(self, item_id: str, patch: Dict[str, Any]) -> Optional[T]:
        existing = await self.collection.find_one({"id": item_id, "_deleted_at": None})
        if existing is None:
            return None
        merged = {key: value for key, value in existing.items() if not key.startswith("_") and key != "_id"}
        merged.update(patch)
        merged["id"] = item_id
        model = self.model_cls.model_validate(merged)
        return await self.save(model)

    async def soft_delete(self, item_id: str) -> bool:
        result = await self.collection.update_one(
            {"id": item_id, "_deleted_at": None},
            {"$set": {"_deleted_at": utc_now().isoformat(), "_updated_at": utc_now().isoformat()}},
        )
        return result.modified_count > 0

    async def restore(self, item_id: str) -> bool:
        result = await self.collection.update_one(
            {"id": item_id},
            {"$set": {"_deleted_at": None, "_updated_at": utc_now().isoformat()}},
        )
        return result.matched_count > 0


class WorkflowCatalogRepository(MongoCatalogRepository[WorkflowDefinition]):
    def __init__(self, db: Optional[AsyncIOMotorDatabase] = None):
        super().__init__(WorkflowDefinition, "workflow_definitions", db)
        self.version_collection = self.collection.database["workflow_versions"]

    def _version_payload(self, workflow: WorkflowDefinition) -> Dict[str, Any]:
        payload = workflow.model_dump(mode="json")
        revision = workflow.versioning.revision
        return {
            "id": f"{workflow.id}:v{revision}",
            "workflow_id": workflow.id,
            "revision": revision,
            "version": workflow.versioning.version,
            "status": "published" if workflow.versioning.is_published else "draft",
            "labels": workflow.versioning.labels,
            "parent_version": workflow.versioning.parent_version,
            "is_published": workflow.versioning.is_published,
            "definition": payload,
            "created_at": utc_now().isoformat(),
            "published_at": utc_now().isoformat() if workflow.versioning.is_published else None,
            "provenance": workflow.metadata.get("provenance") if isinstance(workflow.metadata, dict) else None,
        }

    async def _remember_version(self, workflow: WorkflowDefinition) -> None:
        payload = self._version_payload(workflow)
        existing = await self.version_collection.find_one({"id": payload["id"]})
        if existing is not None:
            payload["created_at"] = existing.get("created_at")
        await self.version_collection.update_one({"id": payload["id"]}, {"$set": payload}, upsert=True)

    async def create(self, item: WorkflowDefinition) -> WorkflowDefinition:
        created = await super().create(item)
        await self._remember_version(created)
        return created

    async def save(self, item: WorkflowDefinition) -> WorkflowDefinition:
        saved = await super().save(item)
        await self._remember_version(saved)
        return saved

    async def update(self, item_id: str, patch: Dict[str, Any]) -> Optional[WorkflowDefinition]:
        updated = await super().update(item_id, patch)
        if updated is not None:
            await self._remember_version(updated)
        return updated

    async def get_workflow(self, workflow_id: str) -> Optional[WorkflowDefinition]:
        return await self.get(workflow_id)

    async def save_workflow(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        return await self.save(workflow)

    async def list_versions(self, workflow_id: str) -> list[dict[str, Any]]:
        current = await self.get(workflow_id, include_deleted=True)
        if current is None:
            return []
        current_revision = current.versioning.revision
        cursor = self.version_collection.find({"workflow_id": workflow_id}).sort("revision", -1)
        items: list[dict[str, Any]] = []
        async for payload in cursor:
            payload.pop("_id", None)
            payload["is_current"] = payload.get("revision") == current_revision
            items.append(payload)
        return items

    async def get_version(self, workflow_id: str, revision: int) -> dict[str, Any] | None:
        current = await self.get(workflow_id, include_deleted=True)
        if current is None:
            return None
        payload = await self.version_collection.find_one({"workflow_id": workflow_id, "revision": revision})
        if payload is None:
            return None
        payload.pop("_id", None)
        payload["is_current"] = revision == current.versioning.revision
        return payload


class InMemoryWorkflowCatalogRepository(InMemoryCatalogRepository[WorkflowDefinition]):
    def __init__(self):
        super().__init__(WorkflowDefinition)
        self._versions: Dict[str, Dict[int, Dict[str, Any]]] = {}

    def _version_payload(self, workflow: WorkflowDefinition) -> Dict[str, Any]:
        payload = workflow.model_dump(mode="json")
        revision = workflow.versioning.revision
        return {
            "id": f"{workflow.id}:v{revision}",
            "workflow_id": workflow.id,
            "revision": revision,
            "version": workflow.versioning.version,
            "status": "published" if workflow.versioning.is_published else "draft",
            "labels": workflow.versioning.labels,
            "parent_version": workflow.versioning.parent_version,
            "is_published": workflow.versioning.is_published,
            "definition": payload,
            "created_at": utc_now().isoformat(),
            "published_at": utc_now().isoformat() if workflow.versioning.is_published else None,
            "provenance": workflow.metadata.get("provenance") if isinstance(workflow.metadata, dict) else None,
        }

    def _remember_version(self, workflow: WorkflowDefinition) -> None:
        workflow_versions = self._versions.setdefault(workflow.id, {})
        revision = workflow.versioning.revision
        existing = workflow_versions.get(revision)
        payload = self._version_payload(workflow)
        if existing is not None:
            payload["created_at"] = existing.get("created_at")
        workflow_versions[revision] = payload

    async def create(self, item: WorkflowDefinition) -> WorkflowDefinition:
        created = await super().create(item)
        self._remember_version(created)
        return created

    async def save(self, item: WorkflowDefinition) -> WorkflowDefinition:
        saved = await super().save(item)
        self._remember_version(saved)
        return saved

    async def update(self, item_id: str, patch: Dict[str, Any]) -> Optional[WorkflowDefinition]:
        updated = await super().update(item_id, patch)
        if updated is not None:
            self._remember_version(updated)
        return updated

    async def get_workflow(self, workflow_id: str) -> Optional[WorkflowDefinition]:
        return await self.get(workflow_id)

    async def save_workflow(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        return await self.save(workflow)

    async def list_versions(self, workflow_id: str) -> list[dict[str, Any]]:
        current = await self.get(workflow_id, include_deleted=True)
        if current is None:
            return []
        current_revision = current.versioning.revision
        versions = []
        for payload in self._versions.get(workflow_id, {}).values():
            item = dict(payload)
            item["is_current"] = item["revision"] == current_revision
            versions.append(item)
        versions.sort(key=lambda item: item["revision"], reverse=True)
        return versions

    async def get_version(self, workflow_id: str, revision: int) -> dict[str, Any] | None:
        versions = await self.list_versions(workflow_id)
        return next((item for item in versions if item["revision"] == revision), None)


class ModelProfileCatalogRepository(MongoCatalogRepository[ModelProfileDefinition]):
    def __init__(self, db: Optional[AsyncIOMotorDatabase] = None):
        super().__init__(ModelProfileDefinition, "model_profile_definitions", db)

    async def get_profile(self, profile_id: str) -> Optional[ModelProfileDefinition]:
        return await self.get(profile_id)

    async def save_profile(self, profile: ModelProfileDefinition) -> ModelProfileDefinition:
        return await self.save(profile)


class InMemoryModelProfileCatalogRepository(InMemoryCatalogRepository[ModelProfileDefinition]):
    def __init__(self):
        super().__init__(ModelProfileDefinition)

    async def get_profile(self, profile_id: str) -> Optional[ModelProfileDefinition]:
        return await self.get(profile_id)

    async def save_profile(self, profile: ModelProfileDefinition) -> ModelProfileDefinition:
        return await self.save(profile)


@dataclass
class RuntimeAdapterSeed:
    id: str
    name: str
    adapter_type: RuntimeAdapterType
    description: str
    capabilities: List[str]


BUILTIN_RUNTIME_ADAPTERS = [
    RuntimeAdapterSeed(
        id="native",
        name="Native Runtime",
        adapter_type=RuntimeAdapterType.NATIVE,
        description="Framework-free native workflow runtime.",
        capabilities=["linear-workflows", "tool-calling", "execution-events"],
    ),
    RuntimeAdapterSeed(
        id="crewai",
        name="CrewAI Runtime",
        adapter_type=RuntimeAdapterType.CREWAI,
        description="CrewAI compatibility adapter.",
        capabilities=["crewai-compatibility", "multi-agent"],
    ),
]


async def ensure_builtin_runtime_adapters(repo: BaseCatalogRepository[RuntimeAdapterDefinition]) -> None:
    for seed in BUILTIN_RUNTIME_ADAPTERS:
        existing = await repo.get(seed.id, include_deleted=True)
        if existing is None:
            await repo.create(
                RuntimeAdapterDefinition(
                    id=seed.id,
                    name=seed.name,
                    adapter_type=seed.adapter_type,
                    description=seed.description,
                    capabilities=seed.capabilities,
                )
            )
