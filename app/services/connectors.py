from __future__ import annotations

import httpx
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.api.context import ApiContext
from app.core.config import get_settings
from app.core.tls import direct_tls_verify
from app.core.time import utc_now
from app.domain import (
    ConnectorHealthHistoryItem,
    ConnectorHealthHistoryPayload,
    ConnectorHealthHistoryPrunePayload,
    Execution,
    ExecutionEventType,
    ExecutionStatus,
    WorkflowDefinition,
)
from app.integrations.connectors import (
    display_connector_provider_key,
    get_connector_definition,
    normalize_connector_provider_key,
)
from app.integrations.onecli import build_onecli_proxy_url
from app.integrations.secrets import is_onecli_secret_ref, onecli_secret_identifier
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import NativeExecutionState
from app.services.credentials import CredentialService
from app.services.onecli import OneCLIIdentityMappingService

CONNECTOR_AUDIT_WORKFLOW_ID = "connector-test"


@dataclass(slots=True)
class ConnectorService:
    context: ApiContext
    emitter: ExecutionEventEmitter = field(init=False)

    def __post_init__(self) -> None:
        self.emitter = ExecutionEventEmitter(self.context.execution_store)

    def _metadata_value(self, metadata: dict[str, Any], key: str, default: str | None = None) -> str:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return default or ""

    def _request_context(self, token: str, metadata: dict[str, Any]) -> dict[str, str]:
        return {
            "token": token,
            "phone_number_id": self._metadata_value(metadata, "phone_number_id"),
            "api_version": self._metadata_value(metadata, "api_version", "v20.0"),
        }

    def _headers_for(self, auth_scheme: str, token: str) -> dict[str, str] | None:
        if auth_scheme == "none":
            return None
        if auth_scheme == "bearer":
            return {"Authorization": f"Bearer {token}"}
        if auth_scheme == "bot":
            return {"Authorization": f"Bot {token}"}
        return None

    def _onecli_proxy_kwargs(self, agent_token_secret_ref: str | None) -> dict[str, Any]:
        settings = get_settings()
        kwargs: dict[str, Any] = {
            "proxy": build_onecli_proxy_url(settings.onecli_gateway_url, agent_token_secret_ref),
        }
        if settings.onecli_gateway_ca_bundle_path:
            kwargs["verify"] = settings.onecli_gateway_ca_bundle_path
        return kwargs

    async def _onecli_agent_token_context(self, owner_user_id: str) -> dict[str, Any]:
        context = await OneCLIIdentityMappingService(self.context).resolve_agent_token_context(
            owner_user_id=owner_user_id
        )
        if isinstance(context.get("agent_token_secret_ref"), str):
            return context

        settings = get_settings()
        if settings.onecli_agent_token_secret_ref:
            return {
                "agent_token_secret_ref": settings.onecli_agent_token_secret_ref,
                "source": "server_configured_agent_token",
                "owner_user_id": owner_user_id,
            }
        return context

    def _onecli_metadata(self, identifier: str, agent_token_context: dict[str, Any]) -> dict[str, Any]:
        settings = get_settings()
        agent_token_secret_ref = agent_token_context.get("agent_token_secret_ref")
        agent_identity = {
            "mapping": str(agent_token_context.get("source") or "none"),
            "mapping_id": agent_token_context.get("mapping_id"),
            "onecli_agent_id": agent_token_context.get("onecli_agent_id"),
            "owner_user_id": agent_token_context.get("owner_user_id"),
            "workflow_id": agent_token_context.get("workflow_id"),
            "agent_token_secret_ref_configured": isinstance(agent_token_secret_ref, str),
        }
        return {
            "credential_mode": "onecli",
            "onecli": {
                "gateway_url": settings.onecli_gateway_url,
                "connection_ref": identifier,
                "agent_token_secret_ref_configured": isinstance(agent_token_secret_ref, str),
                "agent_identity": agent_identity,
            },
        }

    def _onecli_unsupported_reason(self, health_check: Any) -> str | None:
        if "{token}" in health_check.request.url_template:
            return "OneCLI connector health does not support token-in-URL providers yet."
        if health_check.request.auth_scheme == "none":
            return "OneCLI connector health only supports providers with header-based auth shapes."
        return None

    def _onecli_transport_mode(self, provider: str) -> str:
        definition = get_connector_definition(provider)
        if definition is None:
            return "proxy"
        return definition.onecli_transport_mode

    def _allows_onecli_header_proxy(self, provider: str) -> bool:
        normalized = normalize_connector_provider_key(provider)
        return normalized in {"discord-bot"}

    def _nested_value(self, payload: dict[str, Any], path: tuple[str, ...] | None) -> Any:
        if not path:
            return None
        current: Any = payload
        for part in path:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    def _execute_health_check(
            self,
            provider: str,
            health_check: Any,
            token: str,
            metadata: dict[str, Any],
            *,
            credential_mode: str = "direct",
            onecli_agent_token_secret_ref: str | None = None,
    ) -> dict[str, Any]:
        request_context = self._request_context(token, metadata)
        request_kwargs: dict[str, Any] = {}
        if credential_mode == "onecli":
            request_kwargs.update(self._onecli_proxy_kwargs(onecli_agent_token_secret_ref))
        else:
            request_kwargs["trust_env"] = False
            verify = direct_tls_verify()
            if verify is not None:
                request_kwargs["verify"] = verify
        response = httpx.request(
            method=health_check.request.method,
            url=health_check.request.url_template.format(**request_context),
            headers=None if credential_mode == "onecli" else self._headers_for(health_check.request.auth_scheme, token),
            params=health_check.request.query_params,
            timeout=5.0,
            **request_kwargs,
        )
        payload = response.json()

        ok = response.status_code == 200
        if health_check.success_bool_field:
            ok = ok and bool(payload.get(health_check.success_bool_field))
        if health_check.success_field:
            actual = payload.get(health_check.success_field)
            if health_check.success_value_from_metadata:
                expected = metadata.get(health_check.success_value_from_metadata)
                ok = ok and str(actual or "") == str(expected or "")
            else:
                ok = ok and bool(actual)

        result: dict[str, Any] = {
            "ok": ok,
            "provider": provider,
            "credential_mode": credential_mode,
            "status_code": response.status_code,
            "health": payload,
        }
        if not ok:
            result["error"] = (
                                  payload.get(health_check.error_field) if health_check.error_field else None
                              ) or self._nested_value(payload,
                                                      health_check.error_nested_field) or f"{provider} API returned HTTP {response.status_code}"
        return result

    async def _begin_audit_execution(self, *, credential_id: str, credential_name: str, provider: str,
                                     owner_user_id: str):
        await self._ensure_audit_workflow()
        execution = Execution(
            id=f"connector-test-{uuid4()}",
            workflow_id=CONNECTOR_AUDIT_WORKFLOW_ID,
            runtime_adapter_id="native",
            status=ExecutionStatus.RUNNING,
            trigger_type="manual",
            trigger_payload={"mode": "connector_health_test"},
            input_payload={"credential_id": credential_id, "provider": provider},
            metadata={
                "mode": "connector_health_test",
                "credential_id": credential_id,
                "credential_name": credential_name,
                "provider": provider,
            },
            started_at=utc_now(),
            created_by=owner_user_id,
        )
        state = NativeExecutionState(execution_id=execution.id, workflow_id=execution.workflow_id)
        await self.context.execution_store.save_execution(execution)
        await self.emitter.emit(
            state,
            ExecutionEventType.EXECUTION_CREATED,
            payload={
                "workflow_id": CONNECTOR_AUDIT_WORKFLOW_ID,
                "mode": "connector_health_test",
                "audit": True,
            },
        )
        await self.emitter.emit(
            state,
            ExecutionEventType.TOOL_CALL_STARTED,
            payload={
                "tool_id": "connector-health-check",
                "tool_name": "Connector Health Check",
                "connector_provider": provider,
                "credential_id": credential_id,
                "credential_name": credential_name,
                "audit": True,
            },
        )
        return execution, state

    async def _ensure_audit_workflow(self) -> None:
        existing = await self.context.workflow_repo.get(CONNECTOR_AUDIT_WORKFLOW_ID)
        if existing is not None:
            return

        # Execution rows are FK-bound to workflows even for system audit records.
        # This reserved workflow keeps connector health history queryable without
        # requiring every deployment to seed the row manually.
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id=CONNECTOR_AUDIT_WORKFLOW_ID,
                name="Connector health checks",
                description="System workflow used to retain connector health-test audit history.",
                entrypoint="connector-health-check",
                allowed_runtime_adapter_ids=["native"],
                default_runtime_adapter_id="native",
                metadata={
                    "mode": "connector_health_test",
                    "system": True,
                },
            )
        )

    async def _complete_audit_execution(
            self,
            *,
            execution: Execution,
            state: NativeExecutionState,
            provider: str,
            credential_id: str,
            credential_name: str,
            payload: dict[str, Any],
    ) -> None:
        if payload.get("ok"):
            execution.status = ExecutionStatus.COMPLETED
            execution.output_payload = payload
            execution.completed_at = utc_now()
            await self.context.execution_store.update_execution(execution)
            await self.emitter.emit(
                state,
                ExecutionEventType.TOOL_CALL_COMPLETED,
                payload={
                    "tool_id": "connector-health-check",
                    "tool_name": "Connector Health Check",
                    "connector_provider": provider,
                    "credential_id": credential_id,
                    "credential_name": credential_name,
                    "output": payload,
                    "audit": True,
                },
            )
            return

        execution.status = ExecutionStatus.FAILED
        execution.error = str(payload.get("error") or "Connector health check failed")
        execution.output_payload = payload
        execution.completed_at = utc_now()
        await self.context.execution_store.update_execution(execution)
        await self.emitter.emit(
            state,
            ExecutionEventType.TOOL_CALL_FAILED,
            payload={
                "tool_id": "connector-health-check",
                "tool_name": "Connector Health Check",
                "connector_provider": provider,
                "credential_id": credential_id,
                "credential_name": credential_name,
                "error": execution.error,
                "output": payload,
                "audit": True,
            },
        )

    async def test_credential_for_owner(self, credential_id: str, owner_user_id: str) -> dict[str, Any] | None:
        credential = await CredentialService(self.context).get_credential_for_owner(credential_id, owner_user_id)
        if credential is None:
            return None

        provider_key = credential.provider or "unknown"
        execution, state = await self._begin_audit_execution(
            credential_id=credential.id,
            credential_name=credential.name,
            provider=provider_key,
            owner_user_id=owner_user_id,
        )

        definition = get_connector_definition(credential.provider)
        if definition is None or definition.health_check is None:
            payload = {
                "ok": False,
                "target_type": "credential",
                "target_id": credential.id,
                "credential_name": credential.name,
                "provider": credential.provider,
                "error": "Credential provider is not supported by connector health checks yet.",
            }
            await self._complete_audit_execution(
                execution=execution,
                state=state,
                provider=provider_key,
                credential_id=credential.id,
                credential_name=credential.name,
                payload=payload,
            )
            return {
                "audit_execution_id": execution.id,
                **payload,
            }

        if is_onecli_secret_ref(credential.secret_ref):
            identifier = onecli_secret_identifier(credential.secret_ref)
            settings = get_settings()
            transport_mode = self._onecli_transport_mode(definition.key)
            if not identifier:
                payload = {
                    "ok": False,
                    "target_type": "credential",
                    "target_id": credential.id,
                    "credential_name": credential.name,
                    "provider": definition.key,
                    "secret_source": "onecli",
                    "secret_identifier": identifier,
                    "credential_mode": transport_mode,
                    "error": "OneCLI credential ref is empty.",
                }
                await self._complete_audit_execution(
                    execution=execution,
                    state=state,
                    provider=definition.key,
                    credential_id=credential.id,
                    credential_name=credential.name,
                    payload=payload,
                )
                return {"audit_execution_id": execution.id, **payload}
            if not settings.onecli_enabled:
                payload = {
                    "ok": False,
                    "target_type": "credential",
                    "target_id": credential.id,
                    "credential_name": credential.name,
                    "provider": definition.key,
                    "secret_source": "onecli",
                    "secret_identifier": identifier,
                    "credential_mode": transport_mode,
                    "error": "ONECLI_ENABLED is false.",
                }
                await self._complete_audit_execution(
                    execution=execution,
                    state=state,
                    provider=definition.key,
                    credential_id=credential.id,
                    credential_name=credential.name,
                    payload=payload,
                )
                return {"audit_execution_id": execution.id, **payload}

            if transport_mode == "direct" and not self._allows_onecli_header_proxy(definition.key):
                payload = {
                    "ok": False,
                    "target_type": "credential",
                    "target_id": credential.id,
                    "credential_name": credential.name,
                    "provider": definition.key,
                    "secret_source": "onecli",
                    "secret_identifier": identifier,
                    "credential_mode": "direct",
                    "error": (
                        "Direct transport mode requires a runtime-resolvable secret ref; "
                        "use the Agency runtime secret mirror for direct delivery."
                    ),
                }
                await self._complete_audit_execution(
                    execution=execution,
                    state=state,
                    provider=definition.key,
                    credential_id=credential.id,
                    credential_name=credential.name,
                    payload=payload,
                )
                return {"audit_execution_id": execution.id, **payload}

            unsupported_reason = self._onecli_unsupported_reason(definition.health_check)
            if unsupported_reason:
                payload = {
                    "ok": False,
                    "target_type": "credential",
                    "target_id": credential.id,
                    "credential_name": credential.name,
                    "provider": definition.key,
                    "secret_source": "onecli",
                    "secret_identifier": identifier,
                    "credential_mode": transport_mode,
                    "error": unsupported_reason,
                }
                await self._complete_audit_execution(
                    execution=execution,
                    state=state,
                    provider=definition.key,
                    credential_id=credential.id,
                    credential_name=credential.name,
                    payload=payload,
                )
                return {"audit_execution_id": execution.id, **payload}

            agent_token_context = await self._onecli_agent_token_context(owner_user_id)
            onecli_metadata = self._onecli_metadata(identifier, agent_token_context)
            agent_token_secret_ref = agent_token_context.get("agent_token_secret_ref")
            if not isinstance(agent_token_secret_ref, str):
                agent_token_secret_ref = None

            try:
                payload = {
                    **self._execute_health_check(
                        definition.key,
                        definition.health_check,
                        "",
                        credential.metadata,
                        credential_mode="onecli",
                        onecli_agent_token_secret_ref=agent_token_secret_ref,
                    ),
                    **onecli_metadata,
                }
            except Exception as exc:
                payload = {
                    "ok": False,
                    "provider": definition.key,
                    "secret_source": "onecli",
                    "secret_identifier": identifier,
                    "credential_mode": "onecli",
                    "error": str(exc),
                    **onecli_metadata,
                }

            await self._complete_audit_execution(
                execution=execution,
                state=state,
                provider=definition.key,
                credential_id=credential.id,
                credential_name=credential.name,
                payload=payload,
            )

            return {
                "audit_execution_id": execution.id,
                "target_type": "credential",
                "target_id": credential.id,
                "credential_name": credential.name,
                "secret_source": "onecli",
                "secret_identifier": identifier,
                **payload,
            }

        resolved = await CredentialService(self.context).resolve_credential_secret(credential)
        if resolved.value is None:
            payload = {
                "ok": False,
                "target_type": "credential",
                "target_id": credential.id,
                "credential_name": credential.name,
                "provider": definition.key,
                "secret_source": resolved.source,
                "secret_identifier": resolved.identifier,
                "error": resolved.error,
            }
            await self._complete_audit_execution(
                execution=execution,
                state=state,
                provider=definition.key,
                credential_id=credential.id,
                credential_name=credential.name,
                payload=payload,
            )
            return {
                "audit_execution_id": execution.id,
                **payload,
            }

        try:
            payload = self._execute_health_check(definition.key, definition.health_check, resolved.value,
                                                 credential.metadata)
        except Exception as exc:
            payload = {
                "ok": False,
                "provider": definition.key,
                "error": str(exc),
            }

        await self._complete_audit_execution(
            execution=execution,
            state=state,
            provider=definition.key,
            credential_id=credential.id,
            credential_name=credential.name,
            payload=payload,
        )

        return {
            "audit_execution_id": execution.id,
            "target_type": "credential",
            "target_id": credential.id,
            "credential_name": credential.name,
            "secret_source": resolved.source,
            "secret_identifier": resolved.identifier,
            **payload,
        }

    async def list_credential_history_for_owner(
            self,
            credential_id: str,
            owner_user_id: str,
            *,
            limit: int = 20,
            offset: int = 0,
            status: str | None = None,
            started_after: datetime | None = None,
            started_before: datetime | None = None,
    ) -> ConnectorHealthHistoryPayload | None:
        credential = await CredentialService(self.context).get_credential_for_owner(credential_id, owner_user_id)
        if credential is None:
            return None

        return await self._list_history_for_owner(
            owner_user_id=owner_user_id,
            credential_id=credential_id,
            credential_name_fallback=credential.name,
            provider_fallback=credential.provider or "unknown",
            limit=limit,
            offset=offset,
            status=status,
            started_after=started_after,
            started_before=started_before,
            provider=None,
        )

    async def list_all_history_for_owner(
            self,
            owner_user_id: str,
            *,
            limit: int = 20,
            offset: int = 0,
            status: str | None = None,
            started_after: datetime | None = None,
            started_before: datetime | None = None,
            provider: str | None = None,
    ) -> ConnectorHealthHistoryPayload:
        return await self._list_history_for_owner(
            owner_user_id=owner_user_id,
            credential_id=None,
            credential_name_fallback="",
            provider_fallback="unknown",
            limit=limit,
            offset=offset,
            status=status,
            started_after=started_after,
            started_before=started_before,
            provider=provider,
        )

    async def prune_credential_history_for_owner(
            self,
            credential_id: str,
            owner_user_id: str,
            *,
            status: str | None = None,
            started_before: datetime | None = None,
            keep_latest: int | None = None,
    ) -> ConnectorHealthHistoryPrunePayload | None:
        credential = await CredentialService(self.context).get_credential_for_owner(credential_id, owner_user_id)
        if credential is None:
            return None

        return await self._prune_history_for_owner(
            owner_user_id=owner_user_id,
            credential_id=credential_id,
            status=status,
            started_before=started_before,
            provider=None,
            keep_latest=keep_latest,
        )

    async def prune_all_history_for_owner(
            self,
            owner_user_id: str,
            *,
            status: str | None = None,
            started_before: datetime | None = None,
            provider: str | None = None,
            keep_latest: int | None = None,
    ) -> ConnectorHealthHistoryPrunePayload:
        return await self._prune_history_for_owner(
            owner_user_id=owner_user_id,
            credential_id=None,
            status=status,
            started_before=started_before,
            provider=provider,
            keep_latest=keep_latest,
        )

    def _filter_connector_executions(
            self,
            executions: list[Execution],
            *,
            owner_user_id: str,
            credential_id: str | None,
            status: str | None,
            started_after: datetime | None,
            started_before: datetime | None,
            provider: str | None,
    ) -> list[Execution]:
        connector_executions = [
            execution
            for execution in executions
            if execution.workflow_id == CONNECTOR_AUDIT_WORKFLOW_ID
               and execution.created_by == owner_user_id
               and execution.metadata.get("mode") == "connector_health_test"
               and (credential_id is None or execution.metadata.get("credential_id") == credential_id)
        ]
        if status is not None:
            connector_executions = [
                execution
                for execution in connector_executions
                if execution.status.value == status
            ]
        if started_after is not None:
            connector_executions = [
                execution
                for execution in connector_executions
                if execution.started_at is not None and execution.started_at >= started_after
            ]
        if started_before is not None:
            connector_executions = [
                execution
                for execution in connector_executions
                if execution.started_at is not None and execution.started_at <= started_before
            ]
        if provider is not None:
            normalized_provider = normalize_connector_provider_key(provider)
            connector_executions = [
                execution
                for execution in connector_executions
                if normalize_connector_provider_key(
                    str(execution.metadata.get("provider") or "unknown")) == normalized_provider
            ]
        connector_executions.sort(key=lambda execution: execution.created_at, reverse=True)
        return connector_executions

    async def _list_history_for_owner(
            self,
            *,
            owner_user_id: str,
            credential_id: str | None,
            credential_name_fallback: str,
            provider_fallback: str,
            limit: int,
            offset: int,
            status: str | None,
            started_after: datetime | None,
            started_before: datetime | None,
            provider: str | None,
    ) -> ConnectorHealthHistoryPayload:
        executions = await self.context.execution_store.list_executions()
        connector_executions = self._filter_connector_executions(
            executions,
            owner_user_id=owner_user_id,
            credential_id=credential_id,
            status=status,
            started_after=started_after,
            started_before=started_before,
            provider=provider,
        )
        total = len(connector_executions)
        paged_executions = connector_executions[offset: offset + limit]

        items: list[ConnectorHealthHistoryItem] = []
        for execution in paged_executions:
            events = await self.context.execution_store.list_events(execution.id)
            items.append(
                ConnectorHealthHistoryItem(
                    executionId=execution.id,
                    credentialId=str(execution.metadata.get("credential_id") or credential_id or ""),
                    credentialName=str(execution.metadata.get("credential_name") or credential_name_fallback),
                    provider=str(
                        display_connector_provider_key(
                            str(execution.metadata.get("provider") or provider_fallback)
                        )
                        or "unknown"
                    ),
                    status=execution.status.value,
                    startedAt=execution.started_at,
                    completedAt=execution.completed_at,
                    error=execution.error,
                    eventTypes=[event.event_type.value for event in events],
                )
            )
        return ConnectorHealthHistoryPayload(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
            status=status,
            startedAfter=started_after,
            startedBefore=started_before,
        )

    async def _prune_history_for_owner(
            self,
            *,
            owner_user_id: str,
            credential_id: str | None,
            status: str | None,
            started_before: datetime | None,
            provider: str | None,
            keep_latest: int | None,
    ) -> ConnectorHealthHistoryPrunePayload:
        executions = await self.context.execution_store.list_executions()
        matched_executions = self._filter_connector_executions(
            executions,
            owner_user_id=owner_user_id,
            credential_id=credential_id,
            status=status,
            started_after=None,
            started_before=started_before,
            provider=provider,
        )

        if keep_latest is not None and keep_latest > 0:
            prunable_executions = matched_executions[keep_latest:]
        else:
            prunable_executions = matched_executions

        deleted = 0
        for execution in prunable_executions:
            if await self.context.execution_store.delete_execution(execution.id):
                deleted += 1

        retained = len(matched_executions) - deleted

        return ConnectorHealthHistoryPrunePayload(
            deleted=deleted,
            matched=len(matched_executions),
            retained=retained,
            status=status,
            provider=provider,
            startedBefore=started_before,
            keepLatest=keep_latest,
            credentialId=credential_id,
        )
