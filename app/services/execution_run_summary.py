from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.core.config import get_settings
from app.domain import Execution, ExecutionStatus, MemoryScope, WorkflowDefinition
from app.services.memory import MemoryService

if TYPE_CHECKING:
    from app.api.context import ApiContext


@dataclass(slots=True)
class ExecutionRunSummaryService:
    context: ApiContext

    async def maybe_persist_run_summary(
            self,
            *,
            execution: Execution,
            workflow: WorkflowDefinition,
            source: str = "execution_run_summary",
            extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        config = self._persistent_run_summary_config(workflow)
        if not settings.agent_persistent_run_summary_enabled or not config.get("enabled"):
            return {"status": "disabled"}
        if not self._is_terminal_eligible(execution, config=config):
            return {"status": "skipped", "reason": "execution_not_eligible"}

        scope = str(config.get("scope") or MemoryScope.WORKFLOW.value)
        scope_payload = self._resolve_scope_payload(scope=scope, execution=execution, workflow=workflow)
        if scope_payload is None:
            return {"status": "skipped", "reason": "missing_scope_requirements"}

        content, summary, metadata = await self._build_summary_payload(execution=execution, workflow=workflow)
        dedupe_key = metadata["dedupe_key"]
        if await self._has_duplicate_recent_summary(
                dedupe_key=dedupe_key,
                workflow=workflow,
                scope=scope,
                scope_payload=scope_payload,
        ):
            return {"status": "skipped", "reason": "duplicate"}

        primary_agent_id = workflow.agent_definitions[0].id if workflow.agent_definitions else None
        scope_metadata = dict(scope_payload.pop("metadata", {}))
        payload = {
            "scope": scope,
            "content": content,
            "summary": summary,
            "memory_type": "run_summary",
            "status": "active",
            "importance": int(config.get("importance", 55) or 55),
            "workflow_id": workflow.id,
            "agent_id": primary_agent_id,
            "source": source,
            "source_execution_id": execution.id,
            "metadata": {
                **scope_metadata,
                **metadata,
                **(extra_metadata or {}),
            },
            "tags": ["run_summary", "workflow", "execution"],
            "sensitive": False,
            **scope_payload,
        }
        created = await MemoryService(self.context).create_memory(payload, trusted_actor=True)
        return {"status": "created", "memory_id": created.id}

    @staticmethod
    def _persistent_run_summary_config(workflow: WorkflowDefinition) -> dict[str, Any]:
        config = workflow.metadata.get("persistent_run_summary")
        return config if isinstance(config, dict) else {}

    @staticmethod
    def _is_terminal_eligible(execution: Execution, *, config: dict[str, Any]) -> bool:
        if execution.status == ExecutionStatus.COMPLETED:
            return bool(execution.output_payload)
        if execution.status == ExecutionStatus.FAILED:
            return bool(config.get("store_failures", False)) and bool(execution.error)
        return False

    @staticmethod
    def _resolve_scope_payload(
            *,
            scope: str,
            execution: Execution,
            workflow: WorkflowDefinition,
    ) -> dict[str, Any] | None:
        owner_ids = workflow.metadata.get("owner_ids")
        owner_ids = owner_ids if isinstance(owner_ids, list) else []
        created_by = workflow.metadata.get("created_by") or execution.created_by
        if scope == MemoryScope.WORKFLOW.value:
            return {
                "workflow_id": workflow.id,
                "metadata": {
                    "created_by": created_by,
                    "owner_ids": owner_ids,
                },
            }
        if scope == MemoryScope.WORKSPACE.value:
            workspace_id = execution.metadata.get("workspace_id") or workflow.metadata.get("workspace_id")
            if not workspace_id:
                return None
            return {
                "workspace_id": workspace_id,
                "metadata": {
                    "created_by": created_by,
                    "owner_ids": owner_ids,
                },
            }
        if scope == MemoryScope.USER.value:
            if not execution.created_by:
                return None
            return {"created_by_user_id": execution.created_by}
        return None

    async def _build_summary_payload(
            self,
            *,
            execution: Execution,
            workflow: WorkflowDefinition,
    ) -> tuple[str, str, dict[str, Any]]:
        status_text = execution.status.value
        result_text = self._stable_text(execution.output_payload) if execution.output_payload else (
                    execution.error or "")
        summary = self._truncate(
            f"{workflow.name} {status_text}: {result_text}" if result_text else f"{workflow.name} {status_text}.",
            180,
        )
        content_lines = [
            f"Workflow {workflow.name} ({workflow.id}) finished with status {status_text}.",
        ]
        if execution.output_payload:
            content_lines.append(f"Output: {self._truncate(self._stable_text(execution.output_payload), 500)}")
        if execution.error:
            content_lines.append(f"Error: {self._truncate(execution.error, 300)}")
        artifacts = await self.context.execution_store.list_artifacts(execution.id)
        artifact_names = [artifact.name for artifact in artifacts]
        if artifact_names:
            content_lines.append("Artifacts: " + ", ".join(artifact_names[:10]))
        normalized = json.dumps(
            {
                "status": status_text,
                "output": execution.output_payload,
                "error": execution.error,
            },
            sort_keys=True,
            default=str,
        )
        dedupe_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        metadata = {
            "summary_version": "v1",
            "execution_status": status_text,
            "workflow_name": workflow.name,
            "agent_ids": [agent.id for agent in workflow.agent_definitions],
            "artifact_names": artifact_names,
            "findings": [],
            "decision_refs": [],
            "dedupe_key": f"run-summary:{workflow.id}:{status_text}:{dedupe_hash}",
        }
        return "\n".join(content_lines), summary, metadata

    async def _has_duplicate_recent_summary(
            self,
            *,
            dedupe_key: str,
            workflow: WorkflowDefinition,
            scope: str,
            scope_payload: dict[str, Any],
    ) -> bool:
        query_kwargs: dict[str, Any] = {
            "memory_types": ["run_summary"],
            "statuses": ["active"],
            "limit": 20,
        }
        if scope == MemoryScope.WORKFLOW.value:
            query_kwargs["workflow_id"] = workflow.id
        elif scope == MemoryScope.WORKSPACE.value:
            query_kwargs["workspace_id"] = scope_payload.get("workspace_id")
        elif scope == MemoryScope.USER.value:
            query_kwargs["user_id"] = scope_payload.get("created_by_user_id")
        candidates = await self.context.memory_repo.query(**query_kwargs)
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        for item in candidates:
            updated_at = item.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if updated_at < cutoff:
                continue
            if item.metadata.get("dedupe_key") == dedupe_key:
                return True
        return False

    @staticmethod
    def _stable_text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        return json.dumps(value, sort_keys=True, default=str)

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        value = value.strip()
        if len(value) <= limit:
            return value
        return value[: max(limit - 3, 0)].rstrip() + "..."
