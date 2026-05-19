from __future__ import annotations

import asyncio
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from sqlalchemy import select
from typing import Any, Dict, List, Optional, Protocol

from app.core.time import utc_now
from app.db.models import (
    ApprovalRequestORM,
    EXECUTION_ARTIFACTS_COLLECTION,
    EXECUTION_EVENTS_COLLECTION,
    EXECUTIONS_COLLECTION,
    ExecutionArtifactORM,
    ExecutionEventORM,
    ExecutionORM,
    ToolInvocationORM,
)
from app.db.mongo import get_mongodb_db_name, mongo_db_connect
from app.domain import Execution, ExecutionArtifact, ExecutionEvent
from .sql import SQLAlchemyRepository


def _serialize_execution(execution: Execution) -> Dict[str, Any]:
    payload = execution.model_dump(mode="json", by_alias=True)
    payload["error_json"] = execution.error_json
    payload.pop("error", None)
    return payload


def _serialize_event(event: ExecutionEvent) -> Dict[str, Any]:
    return event.model_dump(mode="json", by_alias=True)


def _serialize_artifact(artifact: ExecutionArtifact) -> Dict[str, Any]:
    return artifact.model_dump(mode="json", by_alias=True)


class ExecutionRepository(Protocol):
    async def create_execution(self, execution: Execution) -> Execution: ...

    async def save_execution(self, execution: Execution) -> Execution: ...

    async def update_execution_status(
            self,
            execution_id: str,
            status: str,
            *,
            worker_id: Optional[str] = None,
            started_at: Optional[datetime] = None,
            ended_at: Optional[datetime] = None,
            last_heartbeat_at: Optional[datetime] = None,
            error_json: Optional[Dict[str, Any]] = None,
            output_json: Optional[Dict[str, Any]] = None,
    ) -> Optional[Execution]: ...

    async def get_execution(self, execution_id: str) -> Optional[Execution]: ...

    async def list_executions(self, *, filters: Optional[Dict[str, Any]] = None) -> List[Execution]: ...

    async def delete_execution(self, execution_id: str) -> bool: ...


class ExecutionEventRepository(Protocol):
    async def append_event(self, event: ExecutionEvent) -> ExecutionEvent: ...

    async def list_events(self, execution_id: str) -> List[ExecutionEvent]: ...

    async def list_events_after_sequence(self, execution_id: str, after_sequence: int) -> List[ExecutionEvent]: ...

    async def delete_event(self, event_id: str) -> bool: ...


class ExecutionArtifactRepository(Protocol):
    async def create_artifact(self, artifact: ExecutionArtifact) -> ExecutionArtifact: ...

    async def list_artifacts(self, execution_id: str) -> List[ExecutionArtifact]: ...


class SQLExecutionRepository(SQLAlchemyRepository[ExecutionORM]):
    def __init__(self, session):
        super().__init__(session, ExecutionORM)


class SQLExecutionEventRepository(SQLAlchemyRepository[ExecutionEventORM]):
    def __init__(self, session):
        super().__init__(session, ExecutionEventORM)

    async def list_for_execution(self, execution_id: str) -> list[ExecutionEventORM]:
        result = await self.session.execute(
            select(ExecutionEventORM).where(ExecutionEventORM.execution_id == execution_id).order_by(
                ExecutionEventORM.sequence.asc())
        )
        return list(result.scalars().all())


class SQLExecutionArtifactRepository(SQLAlchemyRepository[ExecutionArtifactORM]):
    def __init__(self, session):
        super().__init__(session, ExecutionArtifactORM)


class ApprovalRequestRepository(SQLAlchemyRepository[ApprovalRequestORM]):
    def __init__(self, session):
        super().__init__(session, ApprovalRequestORM)


class ToolInvocationRepository(SQLAlchemyRepository[ToolInvocationORM]):
    def __init__(self, session):
        super().__init__(session, ToolInvocationORM)


class InMemoryExecutionRepository:
    def __init__(self):
        self._items: Dict[str, Execution] = {}

    async def create_execution(self, execution: Execution) -> Execution:
        execution.updated_at = utc_now()
        self._items[execution.id] = execution
        return execution

    async def save_execution(self, execution: Execution) -> Execution:
        execution.updated_at = utc_now()
        self._items[execution.id] = execution
        return execution

    async def update_execution_status(
            self,
            execution_id: str,
            status: str,
            *,
            worker_id: Optional[str] = None,
            started_at: Optional[datetime] = None,
            ended_at: Optional[datetime] = None,
            last_heartbeat_at: Optional[datetime] = None,
            error_json: Optional[Dict[str, Any]] = None,
            output_json: Optional[Dict[str, Any]] = None,
    ) -> Optional[Execution]:
        execution = self._items.get(execution_id)
        if execution is None:
            return None
        execution.status = execution.status.__class__(status)
        execution.updated_at = utc_now()
        if worker_id is not None:
            execution.worker_id = worker_id
        if started_at is not None:
            execution.started_at = started_at
        if ended_at is not None:
            execution.completed_at = ended_at
        if last_heartbeat_at is not None:
            execution.last_heartbeat_at = last_heartbeat_at
        if error_json is not None:
            execution.error = error_json.get("message") or error_json.get("error") or str(error_json)
        if output_json is not None:
            execution.output_payload = output_json
        self._items[execution_id] = execution
        return execution

    async def get_execution(self, execution_id: str) -> Optional[Execution]:
        return self._items.get(execution_id)

    async def list_executions(self, *, filters: Optional[Dict[str, Any]] = None) -> List[Execution]:
        items = list(self._items.values())
        if not filters:
            return items
        results = []
        for execution in items:
            matched = True
            for key, expected in filters.items():
                if key == "status_in":
                    if execution.status.value not in expected:
                        matched = False
                        break
                    continue
                current = getattr(execution, key)
                if current != expected:
                    matched = False
                    break
            if matched:
                results.append(execution)
        return results

    async def delete_execution(self, execution_id: str) -> bool:
        return self._items.pop(execution_id, None) is not None


class InMemoryExecutionEventRepository:
    def __init__(self):
        self._items: Dict[str, List[ExecutionEvent]] = {}
        self._sequences: Dict[str, int] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock_for(self, execution_id: str) -> asyncio.Lock:
        if execution_id not in self._locks:
            self._locks[execution_id] = asyncio.Lock()
        return self._locks[execution_id]

    async def append_event(self, event: ExecutionEvent) -> ExecutionEvent:
        async with self._lock_for(event.execution_id):
            next_sequence = self._sequences.get(event.execution_id, 0) + 1
            self._sequences[event.execution_id] = next_sequence
            event.sequence = next_sequence
            self._items.setdefault(event.execution_id, []).append(event)
            return event

    async def list_events(self, execution_id: str) -> List[ExecutionEvent]:
        return list(self._items.get(execution_id, []))

    async def list_events_after_sequence(self, execution_id: str, after_sequence: int) -> List[ExecutionEvent]:
        return [event for event in self._items.get(execution_id, []) if event.sequence > after_sequence]

    async def delete_event(self, event_id: str) -> bool:
        for execution_id, events in list(self._items.items()):
            remaining = [event for event in events if event.id != event_id]
            if len(remaining) != len(events):
                self._items[execution_id] = remaining
                return True
        return False


class InMemoryExecutionArtifactRepository:
    def __init__(self):
        self._items: Dict[str, List[ExecutionArtifact]] = {}

    async def create_artifact(self, artifact: ExecutionArtifact) -> ExecutionArtifact:
        self._items.setdefault(artifact.execution_id, []).append(artifact)
        return artifact

    async def list_artifacts(self, execution_id: str) -> List[ExecutionArtifact]:
        return list(self._items.get(execution_id, []))


class MongoExecutionRepository:
    def __init__(self, db: Optional[AsyncIOMotorDatabase] = None):
        if db is None:
            client = mongo_db_connect()
            db = client.get_database(get_mongodb_db_name())
        self.collection = db[EXECUTIONS_COLLECTION.name]

    async def create_execution(self, execution: Execution) -> Execution:
        payload = _serialize_execution(execution)
        payload["_next_event_sequence"] = 0
        await self.collection.insert_one(payload)
        return execution

    async def save_execution(self, execution: Execution) -> Execution:
        payload = _serialize_execution(execution)
        existing = await self.collection.find_one({"id": execution.id}, {"_next_event_sequence": 1})
        if existing and "_next_event_sequence" in existing:
            payload["_next_event_sequence"] = existing["_next_event_sequence"]
        else:
            payload["_next_event_sequence"] = 0
        await self.collection.update_one({"id": execution.id}, {"$set": payload}, upsert=True)
        return execution

    async def update_execution_status(
            self,
            execution_id: str,
            status: str,
            *,
            worker_id: Optional[str] = None,
            started_at: Optional[datetime] = None,
            ended_at: Optional[datetime] = None,
            last_heartbeat_at: Optional[datetime] = None,
            error_json: Optional[Dict[str, Any]] = None,
            output_json: Optional[Dict[str, Any]] = None,
    ) -> Optional[Execution]:
        patch: Dict[str, Any] = {"status": status, "updated_at": utc_now().isoformat()}
        if worker_id is not None:
            patch["worker_id"] = worker_id
        if started_at is not None:
            patch["started_at"] = started_at.isoformat()
        if ended_at is not None:
            patch["ended_at"] = ended_at.isoformat()
        if last_heartbeat_at is not None:
            patch["last_heartbeat_at"] = last_heartbeat_at.isoformat()
        if error_json is not None:
            patch["error_json"] = error_json
        if output_json is not None:
            patch["output_json"] = output_json
        result = await self.collection.find_one_and_update(
            {"id": execution_id},
            {"$set": patch},
            return_document=ReturnDocument.AFTER,
        )
        if result is None:
            return None
        result.pop("_id", None)
        result.pop("_next_event_sequence", None)
        return Execution.model_validate(result)

    async def get_execution(self, execution_id: str) -> Optional[Execution]:
        record = await self.collection.find_one({"id": execution_id})
        if record is None:
            return None
        record.pop("_id", None)
        record.pop("_next_event_sequence", None)
        return Execution.model_validate(record)

    async def list_executions(self, *, filters: Optional[Dict[str, Any]] = None) -> List[Execution]:
        query: Dict[str, Any] = {}
        if filters:
            for key, expected in filters.items():
                if key == "status_in":
                    query["status"] = {"$in": list(expected)}
                else:
                    query[key] = expected
        cursor = self.collection.find(query).sort("created_at", -1)
        items: List[Execution] = []
        async for record in cursor:
            record.pop("_id", None)
            record.pop("_next_event_sequence", None)
            items.append(Execution.model_validate(record))
        return items

    async def delete_execution(self, execution_id: str) -> bool:
        result = await self.collection.delete_one({"id": execution_id})
        return result.deleted_count > 0


class MongoExecutionEventRepository:
    def __init__(self, execution_repository: MongoExecutionRepository, db: Optional[AsyncIOMotorDatabase] = None):
        if db is None:
            client = mongo_db_connect()
            db = client.get_database(get_mongodb_db_name())
        self.collection = db[EXECUTION_EVENTS_COLLECTION.name]
        self.execution_repository = execution_repository

    async def append_event(self, event: ExecutionEvent) -> ExecutionEvent:
        counter = await self.execution_repository.collection.find_one_and_update(
            {"id": event.execution_id},
            {"$inc": {"_next_event_sequence": 1}, "$set": {"updated_at": utc_now().isoformat()}},
            return_document=ReturnDocument.AFTER,
        )
        if counter is None:
            raise ValueError(f"Execution '{event.execution_id}' was not found for event append")
        event.sequence = int(counter["_next_event_sequence"])
        await self.collection.insert_one(_serialize_event(event))
        return event

    async def list_events(self, execution_id: str) -> List[ExecutionEvent]:
        cursor = self.collection.find({"execution_id": execution_id}).sort("sequence", 1)
        items: List[ExecutionEvent] = []
        async for record in cursor:
            record.pop("_id", None)
            items.append(ExecutionEvent.model_validate(record))
        return items

    async def list_events_after_sequence(self, execution_id: str, after_sequence: int) -> List[ExecutionEvent]:
        cursor = self.collection.find({"execution_id": execution_id, "sequence": {"$gt": after_sequence}}).sort(
            "sequence", 1)
        items: List[ExecutionEvent] = []
        async for record in cursor:
            record.pop("_id", None)
            items.append(ExecutionEvent.model_validate(record))
        return items

    async def delete_event(self, event_id: str) -> bool:
        result = await self.collection.delete_one({"id": event_id})
        return result.deleted_count > 0


class MongoExecutionArtifactRepository:
    def __init__(self, db: Optional[AsyncIOMotorDatabase] = None):
        if db is None:
            client = mongo_db_connect()
            db = client.get_database(get_mongodb_db_name())
        self.collection = db[EXECUTION_ARTIFACTS_COLLECTION.name]

    async def create_artifact(self, artifact: ExecutionArtifact) -> ExecutionArtifact:
        await self.collection.insert_one(_serialize_artifact(artifact))
        return artifact

    async def list_artifacts(self, execution_id: str) -> List[ExecutionArtifact]:
        cursor = self.collection.find({"execution_id": execution_id}).sort("created_at", 1)
        items: List[ExecutionArtifact] = []
        async for record in cursor:
            record.pop("_id", None)
            items.append(ExecutionArtifact.model_validate(record))
        return items
