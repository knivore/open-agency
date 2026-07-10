"""Conversation compaction service for durable context-pack memory records."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.domain import (
    Conversation,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    ExecutionEvent,
    ExecutionEventType,
    MemoryType,
    MemoryScope,
    MemoryStatus,
    ModelProfileDefinition,
)
from app.llm.base import ModelMessage
from app.runtime.governance.context_health import estimate_context_health
from app.runtime.governance.recorder import record_context_health_snapshot, record_token_usage_snapshot
from app.runtime.governance.token_usage import normalize_token_usage
from app.services.conversations.audit import CONVERSATION_AUDIT_WORKFLOW_ID, ConversationAuditService
from app.services.conversations.core import ConversationNotFoundError
from app.services.memory import MemoryService

COMPACT_TOOL_SOURCE = "compact_tool"
SUPPORTED_COMPACT_STRATEGIES = {"deterministic", "llm", "auto"}
SUPPORTED_COMPACT_SOURCE_RANGES = {"full", "selected", "since_last_compact", "older_than_recent"}
SUPPORTED_COMPACT_SCOPES = {"conversation", "workspace", "workflow", "user"}
SUPPORTED_COMPACT_FORMATS = {"markdown", "json", "markdown_json"}
CUSTOM_SECTION_KEYS = {
    "goals",
    "facts",
    "preferences",
    "decisions",
    "constraints",
    "commitments",
    "open_questions",
    "next_actions",
    "artifacts",
    "risks",
    "discarded_approaches",
    "verification_needed",
    "owners",
    "expected_outputs",
}


@dataclass(slots=True)
class ContextPackModeProfile:
    mode: str
    schema_version: str
    importance: int
    default_sections: tuple[str, ...]
    required_structured_fields: tuple[str, ...] = tuple(CUSTOM_SECTION_KEYS)


MODE_PROFILE_REGISTRY: dict[str, ContextPackModeProfile] = {
    "brief": ContextPackModeProfile(
        mode="brief",
        schema_version="context_pack.brief.v1",
        importance=45,
        default_sections=("goals", "decisions", "next_actions"),
    ),
    "handoff": ContextPackModeProfile(
        mode="handoff",
        schema_version="context_pack.handoff.v1",
        importance=70,
        default_sections=("goals", "decisions", "constraints", "open_questions", "next_actions", "artifacts"),
    ),
    "memory": ContextPackModeProfile(
        mode="memory",
        schema_version="context_pack.memory.v1",
        importance=65,
        default_sections=("facts", "preferences", "decisions", "commitments", "verification_needed"),
    ),
    "workflow": ContextPackModeProfile(
        mode="workflow",
        schema_version="context_pack.workflow.v1",
        importance=70,
        default_sections=(
            "goals",
            "commitments",
            "next_actions",
            "risks",
            "artifacts",
            "constraints",
            "owners",
            "expected_outputs",
        ),
    ),
    "technical": ContextPackModeProfile(
        mode="technical",
        schema_version="context_pack.technical.v1",
        importance=65,
        default_sections=("goals", "artifacts", "decisions", "constraints", "next_actions", "risks"),
    ),
    "archive": ContextPackModeProfile(
        mode="archive",
        schema_version="context_pack.archive.v1",
        importance=55,
        default_sections=("goals", "decisions", "constraints", "open_questions", "next_actions", "artifacts"),
    ),
    "custom": ContextPackModeProfile(
        mode="custom",
        schema_version="context_pack.custom.v1",
        importance=60,
        default_sections=("goals", "decisions", "constraints", "open_questions", "next_actions", "artifacts"),
    ),
}
SUPPORTED_COMPACT_MODES = set(MODE_PROFILE_REGISTRY)
MODE_LLM_RENDER_PROMPTS: dict[str, str] = {
    "brief": (
        "Render a short human-readable overview. Keep it concise, focus on the latest status and immediate next step, "
        "and avoid low-level implementation detail."
    ),
    "handoff": (
        "Render a handoff pack for another agent or future conversation. Include current state, decisions, "
        "constraints, open questions, next actions, and artifacts."
    ),
    "memory": (
        "Render durable memory candidates cautiously. Separate stable facts, user preferences, decisions, "
        "commitments, and anything that needs verification. Do not promote temporary statements into facts."
    ),
    "workflow": (
        "Render workflow-ready context. Include workflow goal, tasks, blockers, dependencies/artifacts, "
        "owners or target agents, expected outputs, and readiness notes."
    ),
    "technical": (
        "Render technical continuation context. Preserve implementation details, relevant artifacts, decisions, "
        "constraints, next actions, and risks."
    ),
    "archive": (
        "Render a chronological archive digest. Preserve enough chronology for audit and historical lookup, "
        "but remove duplicate or low-signal chatter."
    ),
    "custom": (
        "Render according to the caller's custom keep/drop fields. Keep requested fields explicit and omit dropped "
        "fields from the rendered content."
    ),
}


@dataclass(slots=True)
class ConversationCompactService:
    context: Any

    async def compact_conversation(
            self,
            conversation_id: str,
            *,
            mode: str = "handoff",
            token_budget: int = 1200,
            output_format: str = "markdown",
            source_execution_id: str | None = None,
            source_range: str = "full",
            source_message_start_id: str | None = None,
            source_message_end_id: str | None = None,
            recent_message_limit: int = 8,
            scope: str = "conversation",
            workflow_id: str | None = None,
            persist: bool = True,
            confirmed: bool = False,
            supersede_previous: bool = True,
            idempotency_key: str | None = None,
            strategy: str = "deterministic",
            model_profile_id: str | None = None,
            custom_keep: list[str] | None = None,
            custom_drop: list[str] | None = None,
    ) -> dict[str, Any]:
        progress = [
            self._progress_event("validate_request", "completed", "Compact request validated."),
        ]
        normalized_mode = self._normalize_mode(mode)
        normalized_strategy = self._normalize_strategy(strategy)
        normalized_format = self._normalize_format(output_format)
        normalized_source_range = self._normalize_source_range(
            source_range,
            source_message_start_id=source_message_start_id,
            source_message_end_id=source_message_end_id,
        )
        normalized_scope = self._normalize_scope(scope)
        normalized_custom_keep = self._normalize_custom_keys(custom_keep)
        normalized_custom_drop = self._normalize_custom_keys(custom_drop)
        normalized_idempotency_key = str(idempotency_key).strip() if idempotency_key else None
        conversation = await self._require_conversation(conversation_id)
        normalized_source_execution_id = await self._normalize_source_execution_id(source_execution_id)
        target_fields = await self._target_fields_for_scope(
            conversation,
            scope=normalized_scope,
            workflow_id=workflow_id,
        )
        progress.append(self._progress_event("load_conversation", "completed", "Source conversation loaded."))
        if persist and normalized_idempotency_key:
            existing = await self._context_pack_by_idempotency_key(
                conversation,
                mode=normalized_mode,
                scope=normalized_scope,
                workflow_id=target_fields.get("workflow_id"),
                idempotency_key=normalized_idempotency_key,
            )
            if existing is not None:
                metadata = existing.metadata
                progress.append(
                    self._progress_event("idempotency_lookup", "completed", "Existing context pack reused.")
                )
                return {
                    "status": "existing",
                    "memory_id": existing.id,
                    "mode": normalized_mode,
                    "format": metadata.get("output_format", "markdown"),
                    "scope": existing.scope.value,
                    "source_execution_id": existing.source_execution_id,
                    "source_range": metadata.get("source_range", normalized_source_range),
                    "idempotency_key": normalized_idempotency_key,
                    "content": existing.content,
                    "summary": existing.summary,
                    "structured": metadata.get("structured", {}),
                    "source_message_count": metadata.get("source_message_count", 0),
                    "estimated_source_tokens": metadata.get("estimated_source_tokens", 0),
                    "estimated_compact_tokens": metadata.get("estimated_compact_tokens", 0),
                    "sensitive": existing.sensitive,
                    "warnings": [],
                    "progress": self._progress_payload(progress),
                }
        progress.append(self._progress_event("idempotency_lookup", "completed", "No reusable context pack found."))
        messages = await self.context.conversation_message_repo.list_by_conversation(conversation_id)
        selected = await self._select_messages(
            conversation,
            messages,
            mode=normalized_mode,
            scope=normalized_scope,
            workflow_id=target_fields.get("workflow_id"),
            source_range=normalized_source_range,
            source_message_start_id=source_message_start_id,
            source_message_end_id=source_message_end_id,
            recent_message_limit=recent_message_limit,
        )
        transcript = [
            self._message_to_transcript_item(item)
            for item in selected
            if self._is_compactable(item, mode=normalized_mode)
        ]
        progress.append(
            self._progress_event(
                "select_source",
                "completed",
                "Source messages selected and normalized.",
                selected_messages=len(selected),
                compactable_messages=len(transcript),
            )
        )
        structured = self._extract_structured_state(transcript)
        content = self._render_mode(
            normalized_mode,
            structured,
            transcript,
            token_budget=token_budget,
            custom_keep=normalized_custom_keep,
            custom_drop=normalized_custom_drop,
        )
        summary = self._build_summary(normalized_mode, structured, transcript)
        generation_strategy = "deterministic"
        warnings = self._warnings(selected, transcript)
        progress.append(
            self._progress_event(
                "render_compact",
                "completed",
                "Deterministic compact content rendered.",
                generation_strategy=generation_strategy,
            )
        )
        if normalized_strategy in {"llm", "auto"} and transcript:
            try:
                progress.append(self._progress_event("llm_generate", "started", "LLM compaction started."))
                llm_output = await self._generate_llm_context_pack(
                    conversation=conversation,
                    mode=normalized_mode,
                    transcript=transcript,
                    token_budget=token_budget,
                    model_profile_id=model_profile_id,
                    custom_keep=normalized_custom_keep,
                    custom_drop=normalized_custom_drop,
                    call_metadata={
                        "source_execution_id": normalized_source_execution_id,
                        "source_range": normalized_source_range,
                        "target_scope": normalized_scope,
                        "workflow_id": target_fields.get("workflow_id"),
                        "source_message_count": len(selected),
                        "compactable_message_count": len(transcript),
                    },
                )
                structured = llm_output["structured"]
                content = llm_output["content"]
                summary = llm_output["summary"]
                generation_strategy = "llm"
                progress.append(self._progress_event("llm_generate", "completed", "LLM compaction completed."))
            except Exception as exc:
                warnings.append(f"LLM compaction fallback used: {exc}")
                progress.append(
                    self._progress_event(
                        "llm_generate",
                        "failed",
                        "LLM compaction failed; deterministic fallback retained.",
                        reason=str(exc),
                    )
                )
        content = self._format_output_content(
            output_format=normalized_format,
            markdown_content=content,
            summary=summary,
            structured=structured,
        )
        metadata = self._build_metadata(
            mode=normalized_mode,
            output_format=normalized_format,
            requested_strategy=normalized_strategy,
            generation_strategy=generation_strategy,
            source_range=normalized_source_range,
            target_scope=normalized_scope,
            source_conversation_id=conversation.id,
            source_execution_id=normalized_source_execution_id,
            selected_messages=selected,
            transcript=transcript,
            compact_content=content,
            structured=structured,
            token_budget=token_budget,
            recent_message_limit=recent_message_limit,
            idempotency_key=normalized_idempotency_key,
            custom_keep=normalized_custom_keep,
            custom_drop=normalized_custom_drop,
        )
        memory_service = MemoryService(self.context)
        sensitivity = self._infer_sensitivity(
            memory_service,
            selected_messages=selected,
            compact_content=content,
        )
        metadata.update(sensitivity)
        progress.append(
            self._progress_event(
                "sensitivity_check",
                "completed",
                "Source and compact output sensitivity checked.",
                sensitive=sensitivity["sensitive"],
            )
        )
        response = {
            "status": "preview",
            "memory_id": None,
            "mode": normalized_mode,
            "format": normalized_format,
            "scope": normalized_scope,
            "source_execution_id": normalized_source_execution_id,
            "source_range": normalized_source_range,
            "idempotency_key": normalized_idempotency_key,
            "content": content,
            "summary": summary,
            "structured": structured,
            "source_message_count": len(selected),
            "estimated_source_tokens": metadata["estimated_source_tokens"],
            "estimated_compact_tokens": metadata["estimated_compact_tokens"],
            "sensitive": sensitivity["sensitive"],
            "warnings": warnings,
            "progress": self._progress_payload(progress),
        }
        if not persist:
            progress.append(self._progress_event("persist", "skipped", "Persistence disabled for preview."))
            response["progress"] = self._progress_payload(progress)
            return response

        metadata.update(target_fields.get("metadata", {}))
        payload = {
            "scope": normalized_scope,
            "source_conversation_id": conversation.id,
            "source_execution_id": normalized_source_execution_id,
            "content": content,
            "summary": summary,
            "created_by_user_id": conversation.created_by_user_id,
            "workspace_id": conversation.workspace_id,
            "source": COMPACT_TOOL_SOURCE,
            "memory_type": MemoryType.CONTEXT_PACK.value,
            "status": MemoryStatus.ACTIVE.value,
            "importance": self._importance_for_mode(normalized_mode),
            "sensitive": sensitivity["sensitive"],
            "metadata": metadata,
            "tags": ["context_pack", normalized_scope, normalized_mode],
        }
        payload.update({key: value for key, value in target_fields.items() if key != "metadata"})
        previous = []
        if supersede_previous:
            previous = await self._active_context_packs(
                conversation,
                mode=normalized_mode,
                scope=normalized_scope,
                workflow_id=target_fields.get("workflow_id"),
            )
            progress.append(
                self._progress_event(
                    "supersede_lookup",
                    "completed",
                    "Existing active context packs checked.",
                    active_pack_count=len(previous),
                )
            )
        created = await memory_service.create_memory(payload, confirmed=confirmed, trusted_actor=True)
        for item in previous:
            if item.id == created.id:
                continue
            await memory_service.mark_memory_superseded(
                memory_id=item.id,
                superseded_by_memory_id=created.id,
                trusted_actor=True,
            )
        progress.append(
            self._progress_event(
                "persist",
                "completed",
                "Context pack persisted.",
                memory_id=created.id,
                superseded_count=len([item for item in previous if item.id != created.id]),
            )
        )
        response["status"] = "created"
        response["memory_id"] = created.id
        response["progress"] = self._progress_payload(progress)
        return response

    async def list_compact_packs(
            self,
            conversation_id: str,
            *,
            mode: str | None = None,
            limit: int = 20,
            include_superseded: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        conversation = await self._require_conversation(conversation_id)
        modes = [self._normalize_mode(mode)] if mode else None
        statuses = None if include_superseded else [MemoryStatus.ACTIVE.value]
        items = await self.context.memory_repo.query(
            scopes=[MemoryScope.CONVERSATION.value],
            conversation_id=conversation.id,
            source=COMPACT_TOOL_SOURCE,
            memory_types=[MemoryType.CONTEXT_PACK.value],
            tags=modes,
            statuses=statuses,
            limit=limit,
        )
        return {"items": [item.model_dump(mode="json") for item in items]}

    async def _require_conversation(self, conversation_id: str) -> Conversation:
        conversation = await self.context.conversation_repo.get(conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(f"Conversation '{conversation_id}' not found")
        return conversation

    async def _normalize_source_execution_id(self, source_execution_id: str | None) -> str | None:
        normalized = str(source_execution_id).strip() if source_execution_id else None
        if not normalized:
            return None
        execution = await self.context.execution_store.get_execution(normalized)
        if execution is None:
            raise ValueError(f"Execution '{normalized}' was not found")
        return normalized

    async def _target_fields_for_scope(
            self,
            conversation: Conversation,
            *,
            scope: str,
            workflow_id: str | None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "target_scope": scope,
        }
        if conversation.created_by_user_id:
            metadata["created_by"] = conversation.created_by_user_id
            metadata["owner_ids"] = [conversation.created_by_user_id]
        if scope == MemoryScope.CONVERSATION.value:
            return {"conversation_id": conversation.id, "metadata": metadata}
        if scope == MemoryScope.USER.value:
            if not conversation.created_by_user_id:
                raise ValueError("user-scoped compact packs require the source conversation to have created_by_user_id")
            return {"created_by_user_id": conversation.created_by_user_id, "metadata": metadata}
        if scope == MemoryScope.WORKSPACE.value:
            if not conversation.workspace_id:
                raise ValueError("workspace-scoped compact packs require the source conversation to have workspace_id")
            return {"workspace_id": conversation.workspace_id, "metadata": metadata}
        if scope == MemoryScope.WORKFLOW.value:
            normalized_workflow_id = str(workflow_id).strip() if workflow_id else None
            if not normalized_workflow_id:
                raise ValueError("workflow-scoped compact packs require workflow_id")
            workflow = await self.context.workflow_repo.get(normalized_workflow_id)
            if workflow is None:
                raise ValueError(f"Workflow '{normalized_workflow_id}' was not found")
            owner_ids = workflow.metadata.get("owner_ids")
            if isinstance(owner_ids, list):
                metadata["owner_ids"] = [item for item in owner_ids if isinstance(item, str)]
            created_by = workflow.metadata.get("created_by")
            if isinstance(created_by, str) and created_by:
                metadata["created_by"] = created_by
            return {
                "workflow_id": normalized_workflow_id,
                "workspace_id": conversation.workspace_id,
                "metadata": metadata,
            }
        raise ValueError(f"Unsupported compact scope '{scope}'")

    @staticmethod
    def _normalize_mode(mode: str | None) -> str:
        normalized = (mode or "handoff").strip().lower()
        if normalized not in MODE_PROFILE_REGISTRY:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_MODES))
            raise ValueError(f"Unsupported compact mode '{mode}'. Choose one of: {allowed}.")
        return normalized

    @staticmethod
    def _mode_profile(mode: str) -> ContextPackModeProfile:
        normalized = ConversationCompactService._normalize_mode(mode)
        return MODE_PROFILE_REGISTRY[normalized]

    @staticmethod
    def _normalize_strategy(strategy: str | None) -> str:
        normalized = (strategy or "deterministic").strip().lower()
        if normalized not in SUPPORTED_COMPACT_STRATEGIES:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_STRATEGIES))
            raise ValueError(f"Unsupported compact strategy '{strategy}'. Choose one of: {allowed}.")
        return normalized

    @staticmethod
    def _normalize_format(output_format: str | None) -> str:
        normalized = (output_format or "markdown").strip().lower().replace("-", "_")
        aliases = {
            "md": "markdown",
            "structured": "json",
            "markdown+json": "markdown_json",
            "markdown_plus_json": "markdown_json",
            "both": "markdown_json",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in SUPPORTED_COMPACT_FORMATS:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_FORMATS))
            raise ValueError(f"Unsupported compact format '{output_format}'. Choose one of: {allowed}.")
        return normalized

    @staticmethod
    def _normalize_scope(scope: str | None) -> str:
        normalized = (scope or "conversation").strip().lower()
        if normalized not in SUPPORTED_COMPACT_SCOPES:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_SCOPES))
            raise ValueError(f"Unsupported compact scope '{scope}'. Choose one of: {allowed}.")
        return normalized

    @staticmethod
    def _normalize_source_range(
            source_range: str | None,
            *,
            source_message_start_id: str | None,
            source_message_end_id: str | None,
    ) -> str:
        normalized = (source_range or "full").strip().lower()
        if normalized == "full" and (source_message_start_id is not None or source_message_end_id is not None):
            normalized = "selected"
        if normalized not in SUPPORTED_COMPACT_SOURCE_RANGES:
            allowed = ", ".join(sorted(SUPPORTED_COMPACT_SOURCE_RANGES))
            raise ValueError(f"Unsupported compact source_range '{source_range}'. Choose one of: {allowed}.")
        return normalized

    @staticmethod
    def _normalize_custom_keys(values: list[str] | None) -> list[str]:
        normalized: list[str] = []
        for value in values or []:
            key = str(value).strip().lower()
            if key in CUSTOM_SECTION_KEYS and key not in normalized:
                normalized.append(key)
        return normalized

    async def _select_messages(
            self,
            conversation: Conversation,
            messages: list[ConversationMessage],
            *,
            mode: str,
            scope: str,
            workflow_id: str | None,
            source_range: str,
            source_message_start_id: str | None,
            source_message_end_id: str | None,
            recent_message_limit: int,
    ) -> list[ConversationMessage]:
        if source_range == "since_last_compact":
            return await self._select_messages_since_last_compact(
                conversation,
                messages,
                mode=mode,
                scope=scope,
                workflow_id=workflow_id,
            )
        if source_range == "older_than_recent":
            recent_count = max(recent_message_limit, 0)
            return messages[:-recent_count] if recent_count else list(messages)
        return self._select_message_range(
            messages,
            start_id=source_message_start_id,
            end_id=source_message_end_id,
        )

    @staticmethod
    def _select_message_range(
            messages: list[ConversationMessage],
            *,
            start_id: str | None,
            end_id: str | None,
    ) -> list[ConversationMessage]:
        start_index = 0
        end_index = len(messages) - 1
        if start_id is not None:
            start_index = next((index for index, item in enumerate(messages) if item.id == start_id), -1)
        if end_id is not None:
            end_index = next((index for index, item in enumerate(messages) if item.id == end_id), -1)
        if start_index < 0 or end_index < 0 or start_index > end_index:
            return []
        return messages[start_index:end_index + 1]

    async def _select_messages_since_last_compact(
            self,
            conversation: Conversation,
            messages: list[ConversationMessage],
            *,
            mode: str,
            scope: str,
            workflow_id: str | None,
    ) -> list[ConversationMessage]:
        previous = await self._active_context_packs(
            conversation,
            mode=mode,
            scope=scope,
            workflow_id=workflow_id,
        )
        last_end_id = next(
            (
                item.metadata.get("source_message_end_id")
                for item in previous
                if item.metadata.get("source_message_end_id")
            ),
            None,
        )
        if not last_end_id:
            return list(messages)
        last_index = next((index for index, item in enumerate(messages) if item.id == last_end_id), -1)
        if last_index < 0:
            return list(messages)
        return messages[last_index + 1:]

    @staticmethod
    def _is_compactable(message: ConversationMessage, *, mode: str = "handoff") -> bool:
        if message.role == ConversationRole.ASSISTANT and message.plain_text:
            cleaned = " ".join(message.plain_text.strip().split())
            if cleaned.startswith("I could not reach the configured LLM for this main agent."):
                return False
            if cleaned.startswith("I received your message:"):
                return False
        if message.message_type == ConversationMessageType.TOOL_RESULT:
            return ConversationCompactService._mode_allows_tool_results(mode)
        return message.message_type in {
            ConversationMessageType.USER_TEXT,
            ConversationMessageType.ASSISTANT_TEXT,
            ConversationMessageType.EXECUTION_STARTED,
            ConversationMessageType.EXECUTION_PROGRESS,
            ConversationMessageType.EXECUTION_COMPLETED,
            ConversationMessageType.APPROVAL_REQUEST,
            ConversationMessageType.APPROVAL_RESULT,
            ConversationMessageType.WORKFLOW_PROPOSAL,
            ConversationMessageType.WORKFLOW_UPDATE_PROPOSAL,
            ConversationMessageType.SYSTEM_NOTE,
        }

    @staticmethod
    def _mode_allows_tool_results(mode: str) -> bool:
        return mode in {"handoff", "workflow", "technical", "archive", "custom"}

    @staticmethod
    def _progress_event(step: str, status: str, message: str, **metadata: Any) -> dict[str, Any]:
        event = {
            "step": step,
            "status": status,
            "message": message,
        }
        if metadata:
            event["metadata"] = metadata
        return event

    @staticmethod
    def _progress_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
        completed = sum(1 for event in events if event["status"] in {"completed", "skipped"})
        failed = sum(1 for event in events if event["status"] == "failed")
        return {
            "completed_steps": completed,
            "failed_steps": failed,
            "events": events,
        }

    @staticmethod
    def _message_to_transcript_item(message: ConversationMessage) -> dict[str, Any]:
        text = message.plain_text or ""
        if not text and message.content:
            text = str(message.content)
        return {
            "id": message.id,
            "role": message.role.value,
            "message_type": message.message_type.value,
            "text": text.strip(),
            "created_at": message.created_at.isoformat(),
        }

    @staticmethod
    def _extract_structured_state(transcript: list[dict[str, Any]]) -> dict[str, Any]:
        user_items = [item for item in transcript if item["role"] == ConversationRole.USER.value and item["text"]]
        assistant_items = [
            item for item in transcript if item["role"] == ConversationRole.ASSISTANT.value and item["text"]
        ]
        operational_items = [
            item for item in transcript
            if item["message_type"] not in {
                ConversationMessageType.USER_TEXT.value,
                ConversationMessageType.ASSISTANT_TEXT.value,
            }
        ]
        return {
            "goals": [ConversationCompactService._truncate(item["text"], 220) for item in user_items[:3]],
            "facts": ConversationCompactService._extract_marker_lines(transcript, ("fact", "source of truth")),
            "preferences": ConversationCompactService._extract_marker_lines(transcript, ("prefer", "preference")),
            "decisions": ConversationCompactService._extract_marker_lines(transcript, ("decided", "decision")),
            "constraints": ConversationCompactService._extract_marker_lines(transcript, ("must", "constraint", "keep")),
            "commitments": ConversationCompactService._extract_marker_lines(transcript, ("todo", "next", "will")),
            "open_questions": ConversationCompactService._extract_questions(user_items),
            "next_actions": [ConversationCompactService._truncate(item["text"], 220) for item in user_items[-3:]],
            "artifacts": ConversationCompactService._extract_artifacts(transcript),
            "risks": ConversationCompactService._extract_marker_lines(transcript, ("risk", "blocker", "issue")),
            "discarded_approaches": [],
            "verification_needed": [],
            "owners": ConversationCompactService._extract_marker_lines(
                transcript,
                ("owner", "assigned", "responsible", "target agent", "agent:"),
            ),
            "expected_outputs": ConversationCompactService._extract_marker_lines(
                transcript,
                ("expected output", "deliverable", "output:", "result should"),
            ),
            "message_counts": {
                "total": len(transcript),
                "user": len(user_items),
                "assistant": len(assistant_items),
                "operational": len(operational_items),
            },
        }

    @staticmethod
    def _extract_marker_lines(transcript: list[dict[str, Any]], markers: tuple[str, ...]) -> list[str]:
        matches: list[str] = []
        for item in transcript:
            lowered = item["text"].lower()
            if item["text"] and any(marker in lowered for marker in markers):
                matches.append(ConversationCompactService._truncate(item["text"], 220))
            if len(matches) >= 6:
                break
        return matches

    @staticmethod
    def _extract_questions(items: list[dict[str, Any]]) -> list[str]:
        questions = [item["text"] for item in items if "?" in item["text"]]
        return [ConversationCompactService._truncate(item, 220) for item in questions[:6]]

    @staticmethod
    def _extract_artifacts(transcript: list[dict[str, Any]]) -> list[str]:
        artifacts: list[str] = []
        for item in transcript:
            text = item["text"]
            for token in text.replace(",", " ").split():
                cleaned = token.strip("`'\"()[]{}")
                if "/" in cleaned or "." in cleaned:
                    if any(
                            cleaned.endswith(ext)
                            for ext in (".py", ".md", ".json", ".yaml", ".yml", ".ts", ".tsx")
                    ):
                        artifacts.append(cleaned)
                if len(artifacts) >= 10:
                    return list(dict.fromkeys(artifacts))
        return list(dict.fromkeys(artifacts))

    @staticmethod
    def _render_mode(
            mode: str,
            structured: dict[str, Any],
            transcript: list[dict[str, Any]],
            *,
            token_budget: int,
            custom_keep: list[str] | None = None,
            custom_drop: list[str] | None = None,
    ) -> str:
        if not transcript:
            return "No compactable conversation messages were found."
        if mode == "brief":
            lines = ["Brief summary", ""]
            latest = transcript[-1]["text"] or "No text content."
            lines.append(ConversationCompactService._truncate(latest, max(token_budget * 4, 300)))
            return "\n".join(lines).strip()
        if mode == "technical":
            sections = [
                ("Technical context", structured["goals"]),
                ("Relevant artifacts", structured["artifacts"]),
                ("Decisions and constraints", [*structured["decisions"], *structured["constraints"]]),
                ("Next actions", structured["next_actions"]),
                ("Risks", structured["risks"]),
            ]
            return ConversationCompactService._render_sections(sections)
        if mode == "memory":
            sections = [
                ("Stable facts", structured["facts"]),
                ("Preferences", structured["preferences"]),
                ("Decisions", structured["decisions"]),
                ("Commitments", structured["commitments"]),
                ("Verification needed", structured["verification_needed"]),
            ]
            return ConversationCompactService._render_sections(sections)
        if mode == "workflow":
            sections = [
                ("Workflow goal", structured["goals"][:2]),
                ("Tasks and commitments", [*structured["commitments"], *structured["next_actions"]]),
                ("Blockers and risks", risks if (risks := structured["risks"]) else structured["open_questions"]),
                ("Dependencies and artifacts", structured["artifacts"]),
                ("Owners and target agents", structured["owners"]),
                ("Expected outputs", structured["expected_outputs"]),
                ("Readiness notes", structured["constraints"]),
            ]
            return ConversationCompactService._render_sections(sections)
        if mode == "archive":
            lines = ["Archive digest", ""]
            for item in transcript[:12]:
                text = ConversationCompactService._truncate(item["text"], 180)
                if text:
                    lines.append(f"- {item['role']} ({item['message_type']}): {text}")
            if len(transcript) > 12:
                lines.append(f"- ... {len(transcript) - 12} additional compactable messages omitted.")
            return "\n".join(lines).strip()
        if mode == "custom":
            section_keys = custom_keep or list(ConversationCompactService._mode_profile(mode).default_sections)
            dropped = set(custom_drop or [])
            sections = [
                (ConversationCompactService._section_title(key), structured.get(key, []))
                for key in section_keys
                if key not in dropped
            ]
            return ConversationCompactService._render_sections(sections)
        sections = [
            ("Current state", structured["goals"][:2] or structured["next_actions"][:1]),
            ("Decisions", structured["decisions"]),
            ("Constraints", structured["constraints"]),
            ("Open questions", structured["open_questions"]),
            ("Next actions", structured["next_actions"]),
            ("Artifacts", structured["artifacts"]),
        ]
        return ConversationCompactService._render_sections(sections)

    @staticmethod
    def _section_title(key: str) -> str:
        return key.replace("_", " ").title()

    @staticmethod
    def _render_sections(sections: list[tuple[str, list[str]]]) -> str:
        lines: list[str] = []
        for heading, values in sections:
            lines.append(heading)
            if values:
                lines.extend(f"- {value}" for value in values)
            else:
                lines.append("- None captured.")
            lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def _format_output_content(
            *,
            output_format: str,
            markdown_content: str,
            summary: str,
            structured: dict[str, Any],
    ) -> str:
        if output_format == "markdown":
            return markdown_content
        json_content = json.dumps(
            {
                "summary": summary,
                "structured": structured,
            },
            ensure_ascii=True,
            indent=2,
        )
        if output_format == "json":
            return json_content
        return f"{markdown_content}\n\n```json\n{json_content}\n```"

    async def _generate_llm_context_pack(
            self,
            *,
            conversation: Conversation,
            mode: str,
            transcript: list[dict[str, Any]],
            token_budget: int,
            model_profile_id: str | None,
            custom_keep: list[str],
            custom_drop: list[str],
            call_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        profile = await self._resolve_compact_model_profile(
            conversation=conversation,
            model_profile_id=model_profile_id,
        )
        client = self.context.llm_provider_registry.resolve(profile)
        schema = self._llm_context_pack_schema()
        messages = [
            ModelMessage(
                role="system",
                content=self._llm_system_prompt(mode),
            ),
            ModelMessage(
                role="user",
                content=(
                    f"Mode: {mode}\n"
                    f"Token budget: {token_budget}\n\n"
                    f"Custom keep fields: {custom_keep or []}\n"
                    f"Custom drop fields: {custom_drop or []}\n\n"
                    f"Mode render instructions: {self._llm_mode_render_prompt(mode)}\n\n"
                    "Return a compact context pack for this transcript as structured JSON matching the schema.\n\n"
                    f"Transcript:\n{json.dumps(transcript, ensure_ascii=True, indent=2)}"
                ),
            ),
        ]
        response = await self._call_structured_compact_model(
            client,
            messages=messages,
            schema=schema,
            profile=profile,
            token_budget=token_budget,
            conversation=conversation,
            mode=mode,
            call_metadata=call_metadata,
        )
        try:
            return self._validate_llm_context_pack(response.content)
        except ValueError as exc:
            repair_messages = [
                *messages,
                ModelMessage(
                    role="assistant",
                    content=json.dumps(response.content, ensure_ascii=True)
                    if isinstance(response.content, (dict, list))
                    else str(response.content),
                ),
                ModelMessage(
                    role="user",
                    content=(
                        "The previous response did not match the required context-pack schema. "
                        f"Error: {exc}. Regenerate only valid structured JSON with summary, content, "
                        "and the complete structured object."
                    ),
                ),
            ]
            repaired = await self._call_structured_compact_model(
                client,
                messages=repair_messages,
                schema=schema,
                profile=profile,
                token_budget=token_budget,
                conversation=conversation,
                mode=mode,
                call_metadata={**call_metadata, "repair": True},
            )
            return self._validate_llm_context_pack(repaired.content)

    @staticmethod
    def _llm_system_prompt(mode: str) -> str:
        return (
            "You compact Agency conversations into reusable context packs. "
            "Preserve current state, decisions, constraints, open questions, next actions, artifacts, "
            "risks, owners, expected outputs, and verification needs when present. Do not invent facts. "
            f"{ConversationCompactService._llm_mode_render_prompt(mode)}"
        )

    @staticmethod
    def _llm_mode_render_prompt(mode: str) -> str:
        return MODE_LLM_RENDER_PROMPTS.get(mode, MODE_LLM_RENDER_PROMPTS["handoff"])

    async def _call_structured_compact_model(
            self,
            client: Any,
            *,
            messages: list[ModelMessage],
            schema: dict[str, Any],
            profile: ModelProfileDefinition,
            token_budget: int,
            conversation: Conversation,
            mode: str,
            call_metadata: dict[str, Any],
    ):
        model_request_id = str(uuid4())
        agent_id = await self._conversation_agent_id(conversation)
        max_tokens = profile.max_tokens or token_budget
        context_health = estimate_context_health(
            messages,
            model_profile=profile,
            reserved_completion_tokens=max_tokens,
        )
        base_payload = {
            "call_kind": "conversation_compaction",
            "compaction_mode": mode,
            "model_profile_id": profile.id,
            "provider": profile.provider,
            "model": profile.model,
            "message_count": len(messages),
            "schema_name": "conversation_context_pack",
            "token_budget": token_budget,
            **call_metadata,
        }
        context_event = await self._emit_compaction_audit_event(
            conversation_id=conversation.id,
            event_type=ExecutionEventType.CONTEXT_HEALTH_RECORDED,
            payload={
                **context_health.model_dump(mode="json"),
                **base_payload,
            },
            metrics={
                "estimated_prompt_tokens": context_health.estimated_prompt_tokens,
                "reserved_completion_tokens": context_health.reserved_completion_tokens,
                "estimated_total_context_tokens": context_health.estimated_total_context_tokens,
                "context_window": context_health.context_window or 0,
                "context_usage_ratio": context_health.usage_ratio or 0,
                "context_status": context_health.status,
            },
            metadata={
                "call_kind": "conversation_compaction",
                "model_profile_id": profile.id,
                "compaction_mode": mode,
            },
            agent_id=agent_id,
            model_request_id=model_request_id,
        )
        audit_execution_id = ConversationAuditService(self.context).audit_execution_id(conversation.id)
        await record_context_health_snapshot(
            self.context.execution_store,
            execution_id=audit_execution_id,
            context_health=context_health,
            agent_id=agent_id,
            event_id=context_event.id if context_event is not None else None,
        )
        await self._emit_compaction_audit_event(
            conversation_id=conversation.id,
            event_type=ExecutionEventType.LLM_REQUEST_CREATED,
            payload={
                **base_payload,
                "context_health": context_health.model_dump(mode="json"),
            },
            metrics={
                "estimated_prompt_tokens": context_health.estimated_prompt_tokens,
                "reserved_completion_tokens": context_health.reserved_completion_tokens,
                "estimated_total_context_tokens": context_health.estimated_total_context_tokens,
                "context_window": context_health.context_window or 0,
                "context_usage_ratio": context_health.usage_ratio or 0,
                "context_status": context_health.status,
            },
            metadata={
                "call_kind": "conversation_compaction",
                "model_profile_id": profile.id,
                "compaction_mode": mode,
            },
            agent_id=agent_id,
            model_request_id=model_request_id,
        )
        if hasattr(client, "agenerate_structured"):
            response = await client.agenerate_structured(
                messages,
                schema=schema,
                schema_name="conversation_context_pack",
                temperature=profile.temperature,
                max_tokens=max_tokens,
            )
        else:
            response = await asyncio.to_thread(
                client.generate_structured,
                messages,
                schema=schema,
                schema_name="conversation_context_pack",
                temperature=profile.temperature,
                max_tokens=max_tokens,
            )
        usage = normalize_token_usage(
            response.usage,
            provider=response.provider or profile.provider,
            model=response.model or profile.model,
            profile=profile,
            estimated_prompt_tokens=context_health.estimated_prompt_tokens,
            response_content=response.content,
        )
        response_event = await self._emit_compaction_audit_event(
            conversation_id=conversation.id,
            event_type=ExecutionEventType.LLM_RESPONSE_CREATED,
            payload={
                **base_payload,
                "response_kind": "conversation_compaction_model_call",
                "usage": usage.model_dump(mode="json"),
            },
            metrics={
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "estimated_cost": usage.estimated_cost,
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
                "token_usage_estimated": usage.estimated,
                "latency_ms": response.latency_ms,
            },
            metadata={
                "call_kind": "conversation_compaction",
                "model_profile_id": profile.id,
                "compaction_mode": mode,
                "provider": usage.provider,
                "model": usage.model,
            },
            agent_id=agent_id,
            model_request_id=model_request_id,
        )
        token_event = await self._emit_compaction_audit_event(
            conversation_id=conversation.id,
            event_type=ExecutionEventType.TOKEN_USAGE_RECORDED,
            payload={
                **base_payload,
                "usage": usage.model_dump(mode="json"),
            },
            metrics={
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "estimated_cost": usage.estimated_cost,
                "token_usage_estimated": usage.estimated,
            },
            metadata={
                "call_kind": "conversation_compaction",
                "model_profile_id": profile.id,
                "compaction_mode": mode,
                "response_event_id": response_event.id if response_event is not None else None,
            },
            agent_id=agent_id,
            model_request_id=model_request_id,
        )
        await record_token_usage_snapshot(
            self.context.execution_store,
            execution_id=audit_execution_id,
            usage=usage,
            agent_id=agent_id,
            workflow_id=CONVERSATION_AUDIT_WORKFLOW_ID,
            model_request_id=model_request_id,
            event_id=token_event.id if token_event is not None else None,
        )
        return response

    async def _conversation_agent_id(self, conversation: Conversation) -> str | None:
        if not conversation.main_agent_profile_id:
            return None
        profile = await self.context.main_agent_profile_repo.get(conversation.main_agent_profile_id)
        return profile.agent_id if profile is not None else None

    async def _emit_compaction_audit_event(
            self,
            *,
            conversation_id: str,
            event_type: ExecutionEventType,
            payload: dict[str, Any] | None = None,
            metadata: dict[str, Any] | None = None,
            metrics: dict[str, Any] | None = None,
            agent_id: str | None = None,
            model_request_id: str | None = None,
    ) -> ExecutionEvent | None:
        try:
            return await ConversationAuditService(self.context).emit(
                conversation_id=conversation_id,
                event_type=event_type,
                payload=payload,
                metadata=metadata,
                metrics=metrics,
                agent_id=agent_id,
                model_request_id=model_request_id,
            )
        except Exception:
            return None

    async def _resolve_compact_model_profile(
            self,
            *,
            conversation: Conversation,
            model_profile_id: str | None,
    ) -> ModelProfileDefinition:
        if model_profile_id:
            profile = await self.context.model_profile_repo.get(model_profile_id)
            if profile is None:
                raise ValueError(f"Model profile '{model_profile_id}' was not found")
            return profile

        if conversation.main_agent_profile_id:
            main_agent_profile = await self.context.main_agent_profile_repo.get(conversation.main_agent_profile_id)
            if main_agent_profile is not None:
                agent = await self.context.agent_repo.get(main_agent_profile.agent_id)
                candidate_id = (
                    agent.model_profile_id
                    if agent is not None and agent.model_profile_id
                    else main_agent_profile.default_model_profile_id
                )
                if candidate_id:
                    profile = await self.context.model_profile_repo.get(candidate_id)
                    if profile is not None:
                        return profile

        profiles = await self.context.model_profile_repo.list()
        structured_profile = next((item for item in profiles if item.supports_structured_output), None)
        if structured_profile is not None:
            return structured_profile
        if profiles:
            return profiles[0]
        raise ValueError("No model profiles are configured for conversation compaction")

    @staticmethod
    def _validate_llm_context_pack(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("structured model response was not an object")
        structured = payload.get("structured")
        if not isinstance(structured, dict):
            raise ValueError("structured model response did not include a structured object")
        normalized_structured = {
            "goals": ConversationCompactService._list_of_strings(structured.get("goals")),
            "facts": ConversationCompactService._list_of_strings(structured.get("facts")),
            "preferences": ConversationCompactService._list_of_strings(structured.get("preferences")),
            "decisions": ConversationCompactService._list_of_strings(structured.get("decisions")),
            "constraints": ConversationCompactService._list_of_strings(structured.get("constraints")),
            "commitments": ConversationCompactService._list_of_strings(structured.get("commitments")),
            "open_questions": ConversationCompactService._list_of_strings(structured.get("open_questions")),
            "next_actions": ConversationCompactService._list_of_strings(structured.get("next_actions")),
            "artifacts": ConversationCompactService._list_of_strings(structured.get("artifacts")),
            "risks": ConversationCompactService._list_of_strings(structured.get("risks")),
            "owners": ConversationCompactService._list_of_strings(structured.get("owners")),
            "expected_outputs": ConversationCompactService._list_of_strings(structured.get("expected_outputs")),
            "discarded_approaches": ConversationCompactService._list_of_strings(
                structured.get("discarded_approaches")
            ),
            "verification_needed": ConversationCompactService._list_of_strings(structured.get("verification_needed")),
        }
        content = str(payload.get("content") or "").strip()
        if not content:
            sections = [
                ("Current state", normalized_structured["goals"]),
                ("Decisions", normalized_structured["decisions"]),
                ("Constraints", normalized_structured["constraints"]),
                ("Open questions", normalized_structured["open_questions"]),
                ("Next actions", normalized_structured["next_actions"]),
                ("Artifacts", normalized_structured["artifacts"]),
            ]
            content = ConversationCompactService._render_sections(sections)
        summary = ConversationCompactService._truncate(str(payload.get("summary") or "").strip(), 180)
        if not summary:
            summary = ConversationCompactService._build_summary("llm", normalized_structured, [])
        return {
            "summary": summary,
            "content": content,
            "structured": normalized_structured,
        }

    @staticmethod
    def _list_of_strings(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()][:12]

    @staticmethod
    def _llm_context_pack_schema() -> dict[str, Any]:
        string_array = {"type": "array", "items": {"type": "string"}}
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "content", "structured"],
            "properties": {
                "summary": {"type": "string"},
                "content": {"type": "string"},
                "structured": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "goals",
                        "facts",
                        "preferences",
                        "decisions",
                        "constraints",
                        "commitments",
                        "open_questions",
                        "next_actions",
                        "artifacts",
                        "risks",
                        "owners",
                        "expected_outputs",
                        "discarded_approaches",
                        "verification_needed",
                    ],
                    "properties": {
                        "goals": string_array,
                        "facts": string_array,
                        "preferences": string_array,
                        "decisions": string_array,
                        "constraints": string_array,
                        "commitments": string_array,
                        "open_questions": string_array,
                        "next_actions": string_array,
                        "artifacts": string_array,
                        "risks": string_array,
                        "owners": string_array,
                        "expected_outputs": string_array,
                        "discarded_approaches": string_array,
                        "verification_needed": string_array,
                    },
                },
            },
        }

    @staticmethod
    def _build_summary(mode: str, structured: dict[str, Any], transcript: list[dict[str, Any]]) -> str:
        first_goal = next((item for item in structured.get("goals", []) if item), None)
        if first_goal:
            return ConversationCompactService._truncate(f"{mode} context: {first_goal}", 180)
        return f"{mode} context pack with {len(transcript)} compactable messages."

    @staticmethod
    def _build_metadata(
            *,
            mode: str,
            output_format: str,
            requested_strategy: str,
            generation_strategy: str,
            source_range: str,
            target_scope: str,
            source_conversation_id: str,
            source_execution_id: str | None,
            selected_messages: list[ConversationMessage],
            transcript: list[dict[str, Any]],
            compact_content: str,
            structured: dict[str, Any],
            token_budget: int,
            recent_message_limit: int,
            idempotency_key: str | None,
            custom_keep: list[str],
            custom_drop: list[str],
    ) -> dict[str, Any]:
        source_text = "\n".join(item.plain_text or str(item.content or "") for item in selected_messages)
        estimated_source_tokens = ConversationCompactService._estimate_tokens(source_text)
        estimated_compact_tokens = ConversationCompactService._estimate_tokens(compact_content)
        return {
            "mode": mode,
            "output_format": output_format,
            "requested_strategy": requested_strategy,
            "generation_strategy": generation_strategy,
            "source_range": source_range,
            "target_scope": target_scope,
            "schema_version": ConversationCompactService._mode_profile(mode).schema_version,
            "summary_version": "v1",
            "source_conversation_id": source_conversation_id,
            "source_execution_id": source_execution_id,
            "source_message_start_id": selected_messages[0].id if selected_messages else None,
            "source_message_end_id": selected_messages[-1].id if selected_messages else None,
            "source_message_start_at": selected_messages[0].created_at.isoformat() if selected_messages else None,
            "source_message_end_at": selected_messages[-1].created_at.isoformat() if selected_messages else None,
            "source_message_count": len(selected_messages),
            "compactable_message_count": len(transcript),
            "token_budget": token_budget,
            "recent_message_limit": recent_message_limit,
            "idempotency_key": idempotency_key,
            "custom_keep": custom_keep,
            "custom_drop": custom_drop,
            "estimated_source_tokens": estimated_source_tokens,
            "estimated_compact_tokens": estimated_compact_tokens,
            "compression_ratio": (
                round(estimated_compact_tokens / estimated_source_tokens, 4)
                if estimated_source_tokens
                else 0
            ),
            "structured": structured,
        }

    async def _active_context_packs(
            self,
            conversation: Conversation,
            *,
            mode: str,
            scope: str,
            workflow_id: str | None,
    ) -> list[Any]:
        return await self.context.memory_repo.query(
            **self._context_pack_query_filters(conversation, scope=scope, workflow_id=workflow_id),
            source=COMPACT_TOOL_SOURCE,
            memory_types=[MemoryType.CONTEXT_PACK.value],
            tags=[mode],
            statuses=[MemoryStatus.ACTIVE.value],
            limit=50,
        )

    async def _context_pack_by_idempotency_key(
            self,
            conversation: Conversation,
            *,
            mode: str,
            scope: str,
            workflow_id: str | None,
            idempotency_key: str,
    ) -> Any | None:
        candidates = await self.context.memory_repo.query(
            **self._context_pack_query_filters(conversation, scope=scope, workflow_id=workflow_id),
            source=COMPACT_TOOL_SOURCE,
            memory_types=[MemoryType.CONTEXT_PACK.value],
            tags=[mode],
            limit=100,
        )
        return next(
            (item for item in candidates if item.metadata.get("idempotency_key") == idempotency_key),
            None,
        )

    @staticmethod
    def _context_pack_query_filters(
            conversation: Conversation,
            *,
            scope: str,
            workflow_id: str | None,
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {
            "scopes": [scope],
            "source_conversation_id": conversation.id,
        }
        if scope == MemoryScope.CONVERSATION.value:
            filters["conversation_id"] = conversation.id
        elif scope == MemoryScope.USER.value:
            filters["user_id"] = conversation.created_by_user_id
        elif scope == MemoryScope.WORKSPACE.value:
            filters["workspace_id"] = conversation.workspace_id
        elif scope == MemoryScope.WORKFLOW.value:
            filters["workflow_id"] = workflow_id
        return filters

    @staticmethod
    def _infer_sensitivity(
            memory_service: MemoryService,
            *,
            selected_messages: list[ConversationMessage],
            compact_content: str,
    ) -> dict[str, bool]:
        source_text = "\n".join(item.plain_text or str(item.content or "") for item in selected_messages)
        source_sensitive = memory_service.infer_sensitive(source_text)
        output_sensitive = memory_service.infer_sensitive(compact_content)
        return {
            "sensitive": source_sensitive or output_sensitive,
            "sensitive_source_detected": source_sensitive,
            "sensitive_output_detected": output_sensitive,
        }

    @staticmethod
    def _warnings(selected: list[ConversationMessage], transcript: list[dict[str, Any]]) -> list[str]:
        warnings = []
        if not selected:
            warnings.append("No messages matched the selected source range.")
        elif not transcript:
            warnings.append("The selected source range had no compactable messages.")
        if len(transcript) < len(selected):
            warnings.append("Some selected messages were skipped because their message type is not compactable.")
        return warnings

    @staticmethod
    def _importance_for_mode(mode: str) -> int:
        return ConversationCompactService._mode_profile(mode).importance

    @staticmethod
    def validate_mode_profiles() -> list[str]:
        errors: list[str] = []
        if set(MODE_PROFILE_REGISTRY) != SUPPORTED_COMPACT_MODES:
            errors.append("SUPPORTED_COMPACT_MODES must match MODE_PROFILE_REGISTRY keys.")
        for mode, profile in MODE_PROFILE_REGISTRY.items():
            if profile.mode != mode:
                errors.append(f"Mode profile '{mode}' has mismatched mode '{profile.mode}'.")
            if profile.schema_version != f"context_pack.{mode}.v1":
                errors.append(f"Mode profile '{mode}' has invalid schema_version '{profile.schema_version}'.")
            if not 0 <= profile.importance <= 100:
                errors.append(f"Mode profile '{mode}' importance must be between 0 and 100.")
            invalid_sections = [section for section in profile.default_sections if section not in CUSTOM_SECTION_KEYS]
            if invalid_sections:
                errors.append(f"Mode profile '{mode}' has invalid default sections: {invalid_sections}.")
            missing_required = [
                field for field in profile.required_structured_fields if field not in CUSTOM_SECTION_KEYS
            ]
            if missing_required:
                errors.append(f"Mode profile '{mode}' has invalid required fields: {missing_required}.")
        return errors

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        if not text.strip():
            return 0
        return max(1, len(text) // 4)

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        value = value.strip()
        if len(value) <= limit:
            return value
        return value[: max(limit - 3, 0)].rstrip() + "..."
