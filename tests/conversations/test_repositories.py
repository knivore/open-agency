from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import reset_settings_cache
from app.db.models import Base
from app.db.repositories.domain_sql import (
    SQLConversationApprovalRequestRepository,
    SQLConversationMessageRepository,
    SQLConversationRepository,
)
from app.db.session import get_async_engine, get_session_maker, reset_session_state
from app.domain import ApprovalRequest, ApprovalTargetType, ApprovalType, Conversation, ConversationMessage


class ConversationRepositoriesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "conversation.db"
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.env_patch = patch.dict(
            os.environ,
            {
                "APP_ENV": "test",
                "DATABASE_URL": self.db_url,
            },
            clear=False,
        )
        self.env_patch.start()
        reset_settings_cache()
        reset_session_state()

    async def asyncSetUp(self) -> None:
        engine = get_async_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = get_session_maker()

    async def asyncTearDown(self) -> None:
        engine = get_async_engine(optional=True)
        if engine is not None:
            await engine.dispose()
        reset_session_state()
        reset_settings_cache()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    async def test_conversation_and_messages_persist(self) -> None:
        conversation_repo = SQLConversationRepository(self.session_factory)
        message_repo = SQLConversationMessageRepository(self.session_factory)
        approval_repo = SQLConversationApprovalRequestRepository(self.session_factory)

        created = await conversation_repo.create(
            Conversation(
                id="conversation-1",
                created_by_user_id="user-1",
                channel_type="api",
                metadata={"source": "repo-test"},
            )
        )
        self.assertEqual(created.id, "conversation-1")
        self.assertIsNone(created.title)

        message = await message_repo.create(
            ConversationMessage(
                id="message-1",
                conversation_id=created.id,
                role="user",
                message_type="user_text",
                plain_text="Hello",
                content={"text": "Hello"},
            )
        )
        self.assertEqual(message.conversation_id, created.id)

        loaded = await conversation_repo.get(created.id)
        assert loaded is not None
        self.assertEqual(loaded.metadata["source"], "repo-test")

        messages = await message_repo.list_by_conversation(created.id)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].plain_text, "Hello")

        renamed = await conversation_repo.update(created.id, {"title": "First thread"})
        assert renamed is not None
        self.assertEqual(renamed.title, "First thread")

        approval = await approval_repo.create(
            ApprovalRequest(
                id="approval-1",
                approval_type=ApprovalType.WORKFLOW_EXECUTION,
                target_type=ApprovalTargetType.WORKFLOW,
                target_id="workflow-1",
                requested_by_agent_id="main-agent",
                requested_by_profile_id="main-agent-profile",
                conversation_id=created.id,
                origin_message_id=message.id,
                summary="Run workflow-1",
            )
        )
        self.assertEqual(approval.status.value, "pending")

        approvals = await approval_repo.list_by_conversation(created.id)
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0].summary, "Run workflow-1")

        approved = await approval_repo.update(
            approval.id,
            {"status": "approved", "approved_by_user_id": "user-1", "decision_reason": "Looks good"},
        )
        assert approved is not None
        self.assertEqual(approved.status.value, "approved")


if __name__ == "__main__":
    unittest.main()
