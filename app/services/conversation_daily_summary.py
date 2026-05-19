from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import TYPE_CHECKING, Any

from app.core.config import get_settings
from app.domain import Conversation, ConversationMessage, ConversationMessageType
from app.services.memory import MemoryService

if TYPE_CHECKING:
    from app.api.context import ApiContext


MEANINGFUL_MESSAGE_TYPES = {
    ConversationMessageType.TOOL_RESULT,
    ConversationMessageType.EXECUTION_STARTED,
    ConversationMessageType.EXECUTION_PROGRESS,
    ConversationMessageType.EXECUTION_COMPLETED,
    ConversationMessageType.APPROVAL_REQUEST,
    ConversationMessageType.APPROVAL_RESULT,
    ConversationMessageType.WORKFLOW_PROPOSAL,
    ConversationMessageType.WORKFLOW_UPDATE_PROPOSAL,
}


@dataclass(slots=True)
class ConversationDailySummaryService:
    context: ApiContext

    async def summarize_day(
            self,
            *,
            target_date: date | None = None,
            timezone_name: str | None = None,
            conversation_id: str | None = None,
            dry_run: bool = False,
    ) -> dict[str, Any]:
        settings = get_settings()
        if not dry_run and not settings.memory_daily_summary_enabled:
            return {
                "status": "disabled",
                "target_date": None if target_date is None else target_date.isoformat(),
                "processed": 0,
                "created": 0,
                "skipped": 0,
                "failed": 0,
                "eligible_conversation_ids": [],
            }
        timezone_name = timezone_name or settings.memory_daily_summary_timezone or "UTC"
        zone = ZoneInfo(timezone_name)
        target = target_date or self._resolve_default_target_date(zone)
        window_start_local = datetime.combine(target, time.min, tzinfo=zone)
        window_end_local = datetime.combine(target, time.max, tzinfo=zone)
        window_start_utc = window_start_local.astimezone(timezone.utc)
        window_end_utc = window_end_local.astimezone(timezone.utc)

        conversations = await self._eligible_conversations(conversation_id=conversation_id)
        processed = 0
        created = 0
        skipped = 0
        failed = 0
        eligible_conversation_ids: list[str] = []
        failures: list[dict[str, str]] = []

        for conversation in conversations:
            messages = await self.context.conversation_message_repo.list_by_conversation(conversation.id)
            window_messages = [
                item
                for item in messages
                if self._message_in_window(item, start=window_start_utc, end=window_end_utc)
            ]
            if not self._is_meaningful_day(window_messages):
                skipped += 1
                continue
            if await self._summary_exists(conversation.id, target):
                skipped += 1
                continue
            processed += 1
            eligible_conversation_ids.append(conversation.id)
            if dry_run:
                continue
            try:
                payload = await self._build_summary_payload(
                    conversation=conversation,
                    messages=window_messages,
                    summary_date=target,
                    timezone_name=timezone_name,
                )
                await MemoryService(self.context).create_daily_summary_memory(**payload)
                created += 1
            except Exception as exc:
                failed += 1
                failures.append({"conversation_id": conversation.id, "reason": str(exc)})

        status = "ok"
        if failed and created:
            status = "partial"
        elif failed and not created and processed:
            status = "error"
        elif dry_run:
            status = "dry_run"

        return {
            "status": status,
            "target_date": target.isoformat(),
            "timezone": timezone_name,
            "processed": processed,
            "created": created,
            "skipped": skipped,
            "failed": failed,
            "eligible_conversation_ids": eligible_conversation_ids,
            "failures": failures,
        }

    async def _eligible_conversations(self, *, conversation_id: str | None) -> list[Conversation]:
        if conversation_id is not None:
            conversation = await self.context.conversation_repo.get(conversation_id)
            return [conversation] if conversation is not None else []
        conversations = await self.context.conversation_repo.list()
        return [item for item in conversations if item.main_agent_profile_id]

    async def _summary_exists(self, conversation_id: str, target_date: date) -> bool:
        items = await self.context.memory_repo.query(
            memory_kinds=["daily_summary"],
            source_conversation_id=conversation_id,
            summary_date_from=target_date,
            summary_date_to=target_date,
            limit=1,
        )
        return bool(items)

    async def _build_summary_payload(
            self,
            *,
            conversation: Conversation,
            messages: list[ConversationMessage],
            summary_date: date,
            timezone_name: str,
    ) -> dict[str, Any]:
        main_agent_profile = None
        agent_id = None
        if conversation.main_agent_profile_id:
            main_agent_profile = await self.context.main_agent_profile_repo.get(conversation.main_agent_profile_id)
            if main_agent_profile is not None:
                agent_id = main_agent_profile.agent_id
        text_messages = [
            item.plain_text.strip()
            for item in messages
            if item.message_type in {ConversationMessageType.USER_TEXT, ConversationMessageType.ASSISTANT_TEXT}
            and item.plain_text
            and item.plain_text.strip()
        ]
        first_lines = text_messages[:4]
        summary = self._truncate(
            " | ".join(first_lines) if first_lines else f"Daily summary for conversation {conversation.id}.",
            180,
        )
        content_parts = [
            f"Summary date: {summary_date.isoformat()} ({timezone_name}).",
            f"Conversation {conversation.id} had {len(messages)} relevant messages.",
        ]
        if first_lines:
            content_parts.append("Key excerpts:")
            content_parts.extend(f"- {self._truncate(line, 220)}" for line in first_lines)
        content = "\n".join(content_parts)
        source_message_ids = [item.id for item in messages[:20]]
        user_turns = sum(1 for item in messages if item.message_type == ConversationMessageType.USER_TEXT)
        return {
            "source_conversation_id": conversation.id,
            "summary_date": summary_date,
            "content": content,
            "summary": summary,
            "created_by_user_id": conversation.created_by_user_id,
            "workspace_id": conversation.workspace_id,
            "agent_id": agent_id,
            "archived_window_start": self._message_window_start(summary_date, timezone_name),
            "archived_window_end": self._message_window_end(summary_date, timezone_name),
            "metadata": {
                "idempotency_key": f"daily-summary:{conversation.id}:{summary_date.isoformat()}:v1",
                "summary_version": "v1",
                "source_message_count": len(messages),
                "source_turn_count": user_turns,
                "source_message_ids": source_message_ids,
                "open_loops": [],
                "decision_refs": [],
                "commitment_refs": [],
            },
            "tags": ["daily_summary", "conversation", "memory"],
            "importance": 60,
        }

    @staticmethod
    def _resolve_default_target_date(zone: ZoneInfo) -> date:
        local_now = datetime.now(timezone.utc).astimezone(zone)
        return (local_now - timedelta(days=1)).date()

    @staticmethod
    def _message_in_window(
            message: ConversationMessage,
            *,
            start: datetime,
            end: datetime,
    ) -> bool:
        created_at = message.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return start <= created_at <= end

    @staticmethod
    def _is_meaningful_day(messages: list[ConversationMessage]) -> bool:
        if not messages:
            return False
        user_text_count = sum(1 for item in messages if item.message_type == ConversationMessageType.USER_TEXT)
        text_count = sum(
            1 for item in messages
            if item.message_type in {ConversationMessageType.USER_TEXT, ConversationMessageType.ASSISTANT_TEXT}
            and item.plain_text
            and item.plain_text.strip()
        )
        has_operational_event = any(item.message_type in MEANINGFUL_MESSAGE_TYPES for item in messages)
        return user_text_count >= 1 or text_count >= 2 or has_operational_event

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        value = value.strip()
        if len(value) <= limit:
            return value
        return value[: max(limit - 3, 0)].rstrip() + "..."

    @staticmethod
    def _message_window_start(summary_date: date, timezone_name: str) -> datetime:
        zone = ZoneInfo(timezone_name)
        return datetime.combine(summary_date, time.min, tzinfo=zone).astimezone(timezone.utc)

    @staticmethod
    def _message_window_end(summary_date: date, timezone_name: str) -> datetime:
        zone = ZoneInfo(timezone_name)
        return datetime.combine(summary_date, time.max, tzinfo=zone).astimezone(timezone.utc)


@dataclass(slots=True)
class DailySummaryScheduleCoordinator:
    timezone_name: str
    target_hour: int = 0
    target_minute: int = 15

    def due_target_date(
            self,
            *,
            now: datetime | None = None,
            last_completed_target_date: date | None = None,
    ) -> date | None:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        local_now = current.astimezone(ZoneInfo(self.timezone_name))
        if (local_now.hour, local_now.minute) < (self.target_hour, self.target_minute):
            return None
        target_date = (local_now - timedelta(days=1)).date()
        if last_completed_target_date is not None and last_completed_target_date >= target_date:
            return None
        return target_date
