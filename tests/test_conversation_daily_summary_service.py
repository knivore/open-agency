from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.core.config import reset_settings_cache
from app.domain import (
    Conversation,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    MainAgentProfile,
)
from app.services import ConversationDailySummaryService


class ConversationDailySummaryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = create_test_api_context()
        self.service = ConversationDailySummaryService(self.context)
        await self.context.main_agent_profile_repo.create(
            MainAgentProfile(
                id="main-agent-profile",
                name="Main",
                agent_id="agent-1",
                default_workflow_id="workflow-1",
            )
        )

    async def asyncTearDown(self) -> None:
        os.environ.pop("MEMORY_DAILY_SUMMARY_ENABLED", None)
        os.environ.pop("MEMORY_DAILY_SUMMARY_TIMEZONE", None)
        reset_settings_cache()

    async def _create_conversation(self, conversation_id: str, *, user_id: str = "user-1") -> Conversation:
        conversation = Conversation(
            id=conversation_id,
            created_by_user_id=user_id,
            main_agent_profile_id="main-agent-profile",
            channel_type="api",
            workspace_id="workspace-1",
        )
        return await self.context.conversation_repo.create(conversation)

    async def _create_message(
            self,
            *,
            conversation_id: str,
            message_id: str,
            role: str,
            message_type: str,
            plain_text: str | None,
            created_at: datetime,
    ) -> None:
        await self.context.conversation_message_repo.create(
            ConversationMessage(
                id=message_id,
                conversation_id=conversation_id,
                role=role,
                message_type=message_type,
                plain_text=plain_text,
                content={"text": plain_text} if plain_text is not None else {},
                created_at=created_at,
            )
        )

    async def test_summarize_day_creates_one_daily_summary_for_meaningful_conversation(self) -> None:
        target_date = date(2026, 5, 7)
        conversation = await self._create_conversation("conversation-1")
        await self._create_message(
            conversation_id=conversation.id,
            message_id="message-1",
            role=ConversationRole.USER.value,
            message_type=ConversationMessageType.USER_TEXT.value,
            plain_text="We should lock the memory implementation plan today.",
            created_at=datetime(2026, 5, 7, 9, 0, tzinfo=timezone.utc),
        )
        await self._create_message(
            conversation_id=conversation.id,
            message_id="message-2",
            role=ConversationRole.ASSISTANT.value,
            message_type=ConversationMessageType.ASSISTANT_TEXT.value,
            plain_text="I will turn the plan into an implementation tracker.",
            created_at=datetime(2026, 5, 7, 9, 1, tzinfo=timezone.utc),
        )

        with patch.dict(
                os.environ,
                {
                    "MEMORY_DAILY_SUMMARY_ENABLED": "true",
                    "MEMORY_DAILY_SUMMARY_TIMEZONE": "UTC",
                },
                clear=False,
        ):
            reset_settings_cache()
            result = await self.service.summarize_day(target_date=target_date)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["created"], 1)
        summaries = await self.context.memory_repo.query(
            memory_kinds=["daily_summary"],
            source_conversation_id=conversation.id,
            summary_date_from=target_date,
            summary_date_to=target_date,
        )
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].memory_kind.value, "daily_summary")
        self.assertEqual(summaries[0].metadata["summary_version"], "v1")

    async def test_summarize_day_skips_duplicate_summary(self) -> None:
        target_date = date(2026, 5, 7)
        conversation = await self._create_conversation("conversation-2")
        await self._create_message(
            conversation_id=conversation.id,
            message_id="message-1",
            role=ConversationRole.USER.value,
            message_type=ConversationMessageType.USER_TEXT.value,
            plain_text="Please summarize this day.",
            created_at=datetime(2026, 5, 7, 10, 0, tzinfo=timezone.utc),
        )

        with patch.dict(
                os.environ,
                {
                    "MEMORY_DAILY_SUMMARY_ENABLED": "true",
                    "MEMORY_DAILY_SUMMARY_TIMEZONE": "UTC",
                },
                clear=False,
        ):
            reset_settings_cache()
            first = await self.service.summarize_day(target_date=target_date)
            second = await self.service.summarize_day(target_date=target_date)

        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["skipped"], 1)

    async def test_summarize_day_dry_run_reports_candidates_without_writing(self) -> None:
        target_date = date(2026, 5, 7)
        conversation = await self._create_conversation("conversation-3")
        await self._create_message(
            conversation_id=conversation.id,
            message_id="message-1",
            role=ConversationRole.USER.value,
            message_type=ConversationMessageType.USER_TEXT.value,
            plain_text="Dry-run this summary.",
            created_at=datetime(2026, 5, 7, 11, 0, tzinfo=timezone.utc),
        )

        result = await self.service.summarize_day(target_date=target_date, dry_run=True, timezone_name="UTC")
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["eligible_conversation_ids"], [conversation.id])
        summaries = await self.context.memory_repo.query(
            memory_kinds=["daily_summary"],
            source_conversation_id=conversation.id,
            summary_date_from=target_date,
            summary_date_to=target_date,
        )
        self.assertEqual(summaries, [])

    async def test_summarize_day_isolates_conversation_failures(self) -> None:
        target_date = date(2026, 5, 7)
        good = await self._create_conversation("conversation-good")
        bad = await self._create_conversation("conversation-bad")
        await self._create_message(
            conversation_id=good.id,
            message_id="message-good",
            role=ConversationRole.USER.value,
            message_type=ConversationMessageType.USER_TEXT.value,
            plain_text="This one should summarize.",
            created_at=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
        )
        await self._create_message(
            conversation_id=bad.id,
            message_id="message-bad",
            role=ConversationRole.USER.value,
            message_type=ConversationMessageType.USER_TEXT.value,
            plain_text="This one should fail summarization.",
            created_at=datetime(2026, 5, 7, 12, 5, tzinfo=timezone.utc),
        )

        original = self.service._build_summary_payload

        async def failing_payload(*args, **kwargs):
            conversation = kwargs["conversation"]
            if conversation.id == bad.id:
                raise ValueError("structured generation failed")
            return await original(*args, **kwargs)

        with patch.dict(
                os.environ,
                {
                    "MEMORY_DAILY_SUMMARY_ENABLED": "true",
                    "MEMORY_DAILY_SUMMARY_TIMEZONE": "UTC",
                },
                clear=False,
        ):
            reset_settings_cache()
            with patch.object(ConversationDailySummaryService, "_build_summary_payload", side_effect=failing_payload):
                result = await self.service.summarize_day(target_date=target_date)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["failures"][0]["conversation_id"], bad.id)


if __name__ == "__main__":
    unittest.main()
