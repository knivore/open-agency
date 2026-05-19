from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from app.db.repositories import (
    InMemoryExecutionArtifactRepository,
    InMemoryExecutionEventRepository,
    InMemoryExecutionRepository,
)
from app.domain import Execution, ExecutionArtifact, ExecutionEvent, ExecutionEventType


class ExecutionRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.execution_repo = InMemoryExecutionRepository()
        self.event_repo = InMemoryExecutionEventRepository()
        self.artifact_repo = InMemoryExecutionArtifactRepository()

    async def test_create_execution(self):
        execution = Execution(
            id="exec-1",
            workflow_id="workflow-1",
            runtime_adapter="native",
            status="created",
            trigger_type="manual",
            trigger_payload={"created_by": "tester"},
            input_json={"topic": "hello"},
            created_by="tester",
        )

        created = await self.execution_repo.create_execution(execution)

        self.assertEqual(created.id, "exec-1")
        self.assertEqual(created.runtime_adapter_id, "native")
        self.assertEqual(created.input_payload, {"topic": "hello"})

    async def test_update_execution_status(self):
        execution = Execution(
            id="exec-2",
            workflow_id="workflow-1",
            runtime_adapter="native",
            status="created",
        )
        await self.execution_repo.create_execution(execution)

        updated = await self.execution_repo.update_execution_status(
            "exec-2",
            "running",
            worker_id="worker-1",
            started_at=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(updated)
        self.assertEqual(updated.status.value, "running")
        self.assertEqual(updated.worker_id, "worker-1")
        self.assertIsNotNone(updated.started_at)

    async def test_appending_events_assigns_monotonic_sequences(self):
        async def append(index: int):
            event = ExecutionEvent(
                execution_id="exec-events",
                event_type=ExecutionEventType.AGENT_MESSAGE_CREATED,
                payload_json={"index": index},
            )
            return await self.event_repo.append_event(event)

        appended = await asyncio.gather(*(append(index) for index in range(10)))

        sequences = sorted(event.sequence for event in appended)
        self.assertEqual(sequences, list(range(1, 11)))

    async def test_event_sequence_ordering_and_after_sequence(self):
        for index in range(3):
            await self.event_repo.append_event(
                ExecutionEvent(
                    execution_id="exec-seq",
                    event_type=ExecutionEventType.TOOL_CALL_COMPLETED,
                    payload_json={"index": index},
                )
            )

        events = await self.event_repo.list_events("exec-seq")
        tail = await self.event_repo.list_events_after_sequence("exec-seq", 1)

        self.assertEqual([event.sequence for event in events], [1, 2, 3])
        self.assertEqual([event.sequence for event in tail], [2, 3])

    async def test_create_artifact(self):
        artifact = ExecutionArtifact(
            id="artifact-1",
            execution_id="exec-artifact",
            event_id="event-1",
            artifact_type="json",
            name="result.json",
            content_json={"ok": True},
            mime_type="application/json",
            metadata_json={"source": "test"},
        )

        created = await self.artifact_repo.create_artifact(artifact)
        artifacts = await self.artifact_repo.list_artifacts("exec-artifact")

        self.assertEqual(created.name, "result.json")
        self.assertEqual(artifacts[0].content_json, {"ok": True})
        self.assertEqual(artifacts[0].metadata, {"source": "test"})

    async def test_delete_execution_removes_record(self):
        execution = Execution(
            id="exec-delete",
            workflow_id="workflow-1",
            runtime_adapter="native",
            status="created",
        )
        await self.execution_repo.create_execution(execution)

        deleted = await self.execution_repo.delete_execution("exec-delete")

        self.assertTrue(deleted)
        self.assertIsNone(await self.execution_repo.get_execution("exec-delete"))


if __name__ == "__main__":
    unittest.main()
