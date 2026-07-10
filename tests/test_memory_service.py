from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.api.context import create_test_api_context
from app.domain import Conversation, MemoryRecord, UserDefinition, WorkflowDefinition
from app.services.memory import MemoryService


class MemoryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = create_test_api_context()
        self.service = MemoryService(self.context)
        self.user = UserDefinition(id="user-1", email="user1@example.com")
        await self.context.user_repo.create(self.user)
        self.workflow = WorkflowDefinition(
            id="workflow-1",
            name="Research Workflow",
            entrypoint="node-1",
            metadata={"created_by": "user-1", "owner_ids": ["user-1"]},
        )
        await self.context.workflow_repo.save(self.workflow)
        self.conversation = Conversation(
            id="conversation-1",
            created_by_user_id="user-1",
            workspace_id="workspace-1",
            channel_type="api",
        )
        await self.context.conversation_repo.create(self.conversation)

    async def test_create_daily_summary_and_list_recent_summaries(self) -> None:
        now = datetime.now(timezone.utc)
        created = await self.service.create_daily_summary_memory(
            source_conversation_id=self.conversation.id,
            summary_date=now.date(),
            content="The day focused on locking the memory implementation plan.",
            summary="Locked the implementation plan.",
            created_by_user_id=self.user.id,
            workspace_id=self.conversation.workspace_id,
            agent_id="agent-1",
            archived_window_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
            archived_window_end=now.replace(hour=23, minute=59, second=59, microsecond=0),
            metadata={"summary_version": "v1"},
            tags=["daily_summary"],
        )
        self.assertEqual(created.memory_type.value, "daily_summary")
        self.assertEqual(created.source_conversation_id, self.conversation.id)
        projection_events = await self.context.graph_projection_event_repo.list_events()
        memory_event = next(event for event in projection_events if event.aggregate_id == created.id)
        self.assertEqual(memory_event.event_type, "memory.created")
        self.assertEqual(memory_event.payload["memory_type"], "daily_summary")
        self.assertEqual(memory_event.payload["archived_window_start"], created.archived_window_start.isoformat())
        self.assertEqual(memory_event.payload["archived_window_end"], created.archived_window_end.isoformat())
        self.assertNotIn("content", memory_event.payload)
        self.assertNotIn("embedding", memory_event.payload)

        recent = await self.service.list_recent_summaries(
            conversation_id=self.conversation.id,
            user_id=self.user.id,
            current_user=self.user,
        )
        self.assertEqual([item.id for item in recent], [created.id])

    async def test_context_pack_listing_skips_pack_covered_by_daily_summary_window(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        window_start = now.replace(hour=0, minute=0, second=0)
        window_end = now.replace(hour=23, minute=59, second=59)
        await self.service.create_daily_summary_memory(
            source_conversation_id=self.conversation.id,
            summary_date=now.date(),
            content="Daily summary covers the compact source window.",
            summary="Daily coverage.",
            created_by_user_id=self.user.id,
            workspace_id=self.conversation.workspace_id,
            agent_id="agent-1",
            archived_window_start=window_start,
            archived_window_end=window_end,
            metadata={"summary_version": "v1"},
            tags=["daily_summary"],
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="covered-context-pack",
                scope="conversation",
                conversation_id=self.conversation.id,
                source_conversation_id=self.conversation.id,
                source="compact_tool",
                content="Covered context pack.",
                summary="Covered pack.",
                memory_type="context_pack",
                importance=90,
                metadata={
                    "mode": "handoff",
                    "source_message_start_at": (window_start + timedelta(hours=1)).isoformat(),
                    "source_message_end_at": (window_start + timedelta(hours=2)).isoformat(),
                },
                tags=["context_pack", "conversation", "handoff"],
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="uncovered-context-pack",
                scope="conversation",
                conversation_id=self.conversation.id,
                source_conversation_id=self.conversation.id,
                source="compact_tool",
                content="Uncovered context pack.",
                summary="Uncovered pack.",
                memory_type="context_pack",
                importance=20,
                metadata={
                    "mode": "handoff",
                    "source_message_start_at": (window_start - timedelta(days=2)).isoformat(),
                    "source_message_end_at": (window_start - timedelta(days=2) + timedelta(hours=1)).isoformat(),
                },
                tags=["context_pack", "conversation", "handoff"],
            )
        )

        packs = await self.service.list_context_packs_for_conversation(
            conversation=self.conversation,
            mode="handoff",
            limit=5,
            current_user=self.user,
        )

        self.assertEqual([item.id for item in packs], ["uncovered-context-pack"])

    async def test_mark_memory_superseded_updates_status(self) -> None:
        memory = await self.service.create_memory(
            {
                "id": "memory-1",
                "scope": "user",
                "created_by_user_id": self.user.id,
                "content": "Use concise updates.",
                "memory_type": "fact",
            },
            current_user=self.user,
        )
        updated = await self.service.mark_memory_superseded(
            memory_id=memory.id,
            superseded_by_memory_id="memory-2",
            current_user=self.user,
        )
        assert updated is not None
        self.assertEqual(updated.status.value, "superseded")
        self.assertEqual(updated.supersedes_memory_id, "memory-2")

    async def test_retrieve_for_agent_respects_kind_and_status_filters(self) -> None:
        await self.context.memory_repo.create(
            MemoryRecord(
                id="decision-1",
                scope="workflow",
                workflow_id=self.workflow.id,
                created_by_user_id=self.user.id,
                content="Prefer the workspace memory source for campaign context.",
                memory_type="decision",
                agent_id="agent-1",
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="archived-1",
                scope="workflow",
                workflow_id=self.workflow.id,
                created_by_user_id=self.user.id,
                content="Old archived memory.",
                memory_type="decision",
                status="archived",
                agent_id="agent-1",
            )
        )
        items = await self.service.retrieve_for_agent(
            agent_id="agent-1",
            workflow_id=self.workflow.id,
            query="workspace context",
            include_types=["decision"],
            exclude_statuses=["archived"],
            current_user=self.user,
        )
        self.assertEqual([item.id for item in items], ["decision-1"])

    async def test_list_context_packs_for_agent_scope_returns_visible_non_sensitive_packs(self) -> None:
        await self.context.memory_repo.create(
            MemoryRecord(
                id="workflow-context-pack",
                scope="workflow",
                workflow_id=self.workflow.id,
                created_by_user_id=self.user.id,
                content="Workflow handoff says QA signoff is required before launch.",
                summary="Workflow handoff context.",
                source="compact_tool",
                memory_type="context_pack",
                tags=["context_pack", "workflow", "handoff"],
                metadata={"mode": "handoff"},
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="sensitive-context-pack",
                scope="workflow",
                workflow_id=self.workflow.id,
                created_by_user_id=self.user.id,
                content="The API key is sk-secret.",
                source="compact_tool",
                memory_type="context_pack",
                tags=["context_pack", "workflow", "handoff"],
                metadata={"mode": "handoff"},
                sensitive=True,
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="regular-decision",
                scope="workflow",
                workflow_id=self.workflow.id,
                created_by_user_id=self.user.id,
                content="Regular workflow memory.",
                memory_type="decision",
            )
        )

        packs = await self.service.list_context_packs_for_agent_scope(
            workflow_id=self.workflow.id,
            mode="handoff",
            query="QA launch",
            current_user=self.user,
        )

        self.assertEqual([item.id for item in packs], ["workflow-context-pack"])

    async def test_retrieve_operational_context_buckets_and_excludes_sensitive(self) -> None:
        today = datetime.now(timezone.utc).date()
        await self.context.memory_repo.create(
            MemoryRecord(
                id="decision-1",
                scope="conversation",
                conversation_id=self.conversation.id,
                content="Use DB-backed durable memory as the source of truth.",
                memory_type="decision",
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="commitment-1",
                scope="conversation",
                conversation_id=self.conversation.id,
                content="Prepare phase-by-phase implementation checklist before coding.",
                memory_type="task_commitment",
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="preference-1",
                scope="user",
                created_by_user_id=self.user.id,
                content="User timezone preference is Asia/Singapore.",
                memory_type="preference",
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="legacy-1",
                scope="workspace",
                workspace_id=self.conversation.workspace_id,
                content="Workspace planning prefers concise rollouts.",
                metadata={"owner_ids": [self.user.id], "created_by": self.user.id},
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="summary-1",
                scope="conversation",
                conversation_id=self.conversation.id,
                source_conversation_id=self.conversation.id,
                content="The day focused on designing the DB-first memory architecture.",
                summary="Locked the DB-first memory design.",
                memory_type="daily_summary",
                summary_date=today,
                archived_window_start=datetime.now(timezone.utc) - timedelta(hours=12),
                archived_window_end=datetime.now(timezone.utc),
            )
        )
        await self.context.memory_repo.create(
            MemoryRecord(
                id="sensitive-1",
                scope="user",
                created_by_user_id=self.user.id,
                content="The API key is sk-secret.",
                sensitive=True,
                memory_type="fact",
            )
        )

        context = await self.service.retrieve_operational_context(
            conversation=self.conversation,
            agent_id="agent-1",
            query="memory design timezone",
            current_user=self.user,
        )
        self.assertEqual([item.id for item in context["decisions"]], ["decision-1"])
        self.assertEqual([item.id for item in context["commitments"]], ["commitment-1"])
        fact_ids = [item.id for item in context["facts_and_preferences"]]
        self.assertIn("preference-1", fact_ids)
        self.assertIn("legacy-1", fact_ids)
        self.assertNotIn("sensitive-1", fact_ids)
        self.assertEqual([item.id for item in context["recent_summaries"]], ["summary-1"])


if __name__ == "__main__":
    unittest.main()
