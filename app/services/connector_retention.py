from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.api.context import ApiContext
from app.core.config import Settings, get_settings
from app.core.time import utc_now
from app.domain import ConnectorHealthRetentionRunPayload, ConnectorHealthRetentionStatusPayload, Execution


@dataclass(slots=True)
class ConnectorRetentionService:
    context: ApiContext

    async def run_once(self, settings: Settings | None = None) -> ConnectorHealthRetentionRunPayload:
        settings = settings or get_settings()
        started_before = utc_now() - timedelta(days=settings.connector_health_history_retention_days)
        return await self.run_policy(
            started_before=started_before,
            keep_latest_per_credential=settings.connector_health_history_retention_max_per_credential,
        )

    def get_status(self, settings: Settings | None = None) -> ConnectorHealthRetentionStatusPayload:
        settings = settings or get_settings()
        snapshot = self.context.runtime_operations.snapshot()
        counters = {
            key: value
            for key, value in snapshot.counters.items()
            if key.startswith("connector_retention.")
        }
        last_run = next(
            (
                action
                for action in reversed(snapshot.recent_actions)
                if action.get("action") == "connector_retention_run"
            ),
            None,
        )
        return ConnectorHealthRetentionStatusPayload(
            enabled=settings.connector_health_history_retention_enabled,
            intervalSeconds=settings.connector_health_history_retention_interval_seconds,
            retentionDays=settings.connector_health_history_retention_days,
            maxPerCredential=settings.connector_health_history_retention_max_per_credential,
            counters=counters,
            lastRun=last_run,
        )

    async def run_policy(
            self,
            *,
            started_before: datetime,
            keep_latest_per_credential: int,
    ) -> ConnectorHealthRetentionRunPayload:
        executions = await self.context.execution_store.list_executions()
        connector_executions = [
            execution
            for execution in executions
            if execution.workflow_id == "connector-test"
               and execution.metadata.get("mode") == "connector_health_test"
               and isinstance(execution.metadata.get("credential_id"), str)
               and str(execution.metadata.get("credential_id")).strip()
        ]

        grouped: dict[str, list[Execution]] = {}
        for execution in connector_executions:
            credential_id = str(execution.metadata["credential_id"])
            grouped.setdefault(credential_id, []).append(execution)

        deletable: list[Execution] = []
        retained = 0
        for credential_executions in grouped.values():
            credential_executions.sort(key=self._sort_timestamp, reverse=True)
            retained += min(keep_latest_per_credential, len(credential_executions))
            for index, execution in enumerate(credential_executions):
                if index < keep_latest_per_credential:
                    continue
                execution_started_at = self._execution_started_at(execution)
                if execution_started_at is None or execution_started_at > started_before:
                    retained += 1
                    continue
                deletable.append(execution)

        deleted = 0
        for execution in deletable:
            if await self.context.execution_store.delete_execution(execution.id):
                deleted += 1

        matched = len(deletable)
        report = ConnectorHealthRetentionRunPayload(
            scanned=len(connector_executions),
            matched=matched,
            deleted=deleted,
            retained=retained,
            startedBefore=started_before,
            keepLatestPerCredential=keep_latest_per_credential,
        )
        self.context.runtime_operations.increment("connector_retention.runs")
        self.context.runtime_operations.increment("connector_retention.scanned", report.scanned)
        self.context.runtime_operations.increment("connector_retention.matched", report.matched)
        self.context.runtime_operations.increment("connector_retention.deleted", report.deleted)
        self.context.runtime_operations.increment("connector_retention.retained", report.retained)
        self.context.runtime_operations.record_action(
            "connector_retention_run",
            run_at=utc_now().isoformat(),
            scanned=report.scanned,
            matched=report.matched,
            deleted=report.deleted,
            retained=report.retained,
            started_before=report.startedBefore.isoformat() if report.startedBefore else None,
            keep_latest_per_credential=report.keepLatestPerCredential,
        )
        return report

    def _execution_started_at(self, execution: Execution) -> datetime | None:
        return execution.started_at or execution.created_at

    def _sort_timestamp(self, execution: Execution) -> datetime:
        return self._execution_started_at(execution) or datetime.min
