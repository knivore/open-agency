"""Execution control plane for local and isolated workflow runs.

The control plane coordinates execution records, runtime adapter dispatch,
Docker worker creation, lifecycle events, cancellation, replacement of stale
active runs, and operator-facing repair actions. Route handlers should stay thin
and call this boundary for runtime state transitions.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional
from uuid import uuid4

from app.core.config import get_settings
from app.core.time import utc_now
from app.domain import ExecutionEventType, ExecutionStatus, ExecutionWaitKind, ExecutionWaitStatus
from app.runtime.containers import ContainerRuntimeError, RuntimeContainerSpec
from app.runtime.execution_lifecycle import resolve_execution_runtime_policy
from app.runtime.lifecycle import RuntimeLifecycleEventEmitter, RuntimeContainerState
from app.runtime.native.approvals import ApprovalManager
from app.runtime.native.errors import ExecutionNotFoundError
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import NativeExecutionState
from app.runtime.registry import EXECUTION_HOST_DOCKER, EXECUTION_HOST_LOCAL, RuntimeAdapterRegistry
from app.services.execution_classification import classify_execution_staleness

ACTIVE_REPLACEMENT_STATUSES = {
    "queued",
    "running",
    "waiting_for_input",
    "waiting_for_approval",
    "waiting_for_event",
    "sleeping",
    "paused",
    "cancelling",
}
STALE_REPAIR_STATUSES = {
    "queued",
    "running",
    "waiting_for_input",
    "waiting_for_approval",
    "waiting_for_event",
    "sleeping",
    "paused",
    "cancelling",
}
LIVE_CONTAINER_STATUSES = {"created", "running", "restarting", "paused"}
EXITED_CONTAINER_STATUSES = {"exited", "dead"}
TERMINAL_EXECUTION_STATUSES = {"completed", "failed", "cancelled"}
ONECLI_PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
ONECLI_CA_ENV_NAMES = ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO", "NODE_EXTRA_CA_CERTS")
DIRECT_EXTERNAL_CREDENTIAL_ENV_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "AZURE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "LOCAL_OPENAI_API_KEY",
    }
)


def _reject_terminal_execution_replay(execution) -> None:
    if execution.status.value in TERMINAL_EXECUTION_STATUSES:
        # Retrying under a new identity preserves the terminal audit record and
        # prevents non-idempotent workflow effects from replaying in place.
        raise ValueError(
            f"Execution '{execution.id}' is terminal and must be retried through a replacement execution."
        )


def onecli_worker_environment(settings) -> dict[str, str]:
    if not (settings.onecli_enabled and settings.onecli_force_for_isolated_workers):
        return {}

    node_options = os.getenv("NODE_OPTIONS", "").strip()
    node_bootstrap_path = settings.onecli_node_proxy_bootstrap_path.strip()
    env = {
        "HTTP_PROXY": settings.onecli_gateway_url,
        "HTTPS_PROXY": settings.onecli_gateway_url,
        "http_proxy": settings.onecli_gateway_url,
        "https_proxy": settings.onecli_gateway_url,
        "NO_PROXY": settings.onecli_worker_no_proxy,
        "no_proxy": settings.onecli_worker_no_proxy,
        "ONECLI_ENABLED": "true",
        "ONECLI_GATEWAY_URL": settings.onecli_gateway_url,
        "ONECLI_FORCE_FOR_ISOLATED_WORKERS": "true",
        "ONECLI_AGENT_TOKEN_SECRET_REF_CONFIGURED": str(bool(settings.onecli_agent_token_secret_ref)).lower(),
    }
    if node_bootstrap_path:
        env["NODE_OPTIONS"] = f"{node_options} --require {node_bootstrap_path}".strip()
        env["ONECLI_NODE_PROXY_BOOTSTRAP_PATH"] = node_bootstrap_path
    # Only HTTPS gateways need their private CA injected into worker clients.
    if settings.onecli_gateway_ca_bundle_path and settings.onecli_gateway_url.lower().startswith("https://"):
        ca_path = settings.onecli_gateway_ca_bundle_container_path
        env.update(
            {
                "REQUESTS_CA_BUNDLE": ca_path,
                "SSL_CERT_FILE": ca_path,
                "CURL_CA_BUNDLE": ca_path,
                "GIT_SSL_CAINFO": ca_path,
                "NODE_EXTRA_CA_CERTS": ca_path,
            }
        )
    return env


def onecli_runtime_metadata(settings) -> dict[str, object]:
    return {
        "enabled": settings.onecli_enabled,
        "force_for_isolated_workers": settings.onecli_force_for_isolated_workers,
        "gateway_url": settings.onecli_gateway_url if settings.onecli_enabled else None,
        "gateway_ca_bundle_configured": bool(settings.onecli_gateway_ca_bundle_path),
        "gateway_ca_bundle_container_path": (
            settings.onecli_gateway_ca_bundle_container_path
            if settings.onecli_gateway_ca_bundle_path
            else None
        ),
        "agent_token_secret_ref_configured": bool(settings.onecli_agent_token_secret_ref),
        "worker_egress_mode": settings.onecli_worker_egress_mode,
        "worker_egress_network": (
            settings.onecli_worker_egress_network
            if settings.onecli_worker_egress_mode == "docker_internal_network"
            else None
        ),
        "node_proxy_bootstrap_configured": bool(settings.onecli_node_proxy_bootstrap_path.strip()),
    }


def onecli_worker_network_name(settings) -> str | None:
    if (
            settings.onecli_enabled
            and settings.onecli_force_for_isolated_workers
            and settings.onecli_worker_egress_mode == "docker_internal_network"
    ):
        return settings.onecli_worker_egress_network
    return None


def onecli_worker_enforcement_diagnostics(
        settings,
        env: dict[str, str],
        *,
        network_name: str | None = None,
) -> dict[str, object]:
    proxy_env_required = settings.onecli_enabled and settings.onecli_force_for_isolated_workers
    ca_bundle_required = bool(proxy_env_required and settings.onecli_gateway_ca_bundle_path)
    proxy_env_present = sorted(name for name in ONECLI_PROXY_ENV_NAMES if env.get(name))
    ca_env_present = sorted(name for name in ONECLI_CA_ENV_NAMES if env.get(name))
    missing_proxy_env = sorted(name for name in ONECLI_PROXY_ENV_NAMES if proxy_env_required and not env.get(name))
    missing_ca_env = sorted(name for name in ONECLI_CA_ENV_NAMES if ca_bundle_required and not env.get(name))
    forbidden_env_present = sorted(name for name in DIRECT_EXTERNAL_CREDENTIAL_ENV_NAMES if env.get(name))
    node_proxy_bootstrap_path = env.get("ONECLI_NODE_PROXY_BOOTSTRAP_PATH")
    enforcement_mode = (
        settings.onecli_worker_egress_mode
        if proxy_env_required
        else "not_enforced"
    )
    container_level_egress_controls = (
        "docker_internal_network"
        if enforcement_mode == "docker_internal_network" and network_name
        else "pending"
    )

    return {
        "enabled": settings.onecli_enabled,
        "force_for_isolated_workers": settings.onecli_force_for_isolated_workers,
        "enforcement_mode": enforcement_mode,
        "proxy_env_required": proxy_env_required,
        "proxy_env_present": proxy_env_present,
        "missing_proxy_env": missing_proxy_env,
        "no_proxy_configured": bool(env.get("NO_PROXY") or env.get("no_proxy")),
        "ca_bundle_required": ca_bundle_required,
        "ca_bundle_configured": bool(settings.onecli_gateway_ca_bundle_path),
        "ca_bundle_container_path": (
            settings.onecli_gateway_ca_bundle_container_path
            if settings.onecli_gateway_ca_bundle_path
            else None
        ),
        "ca_env_present": ca_env_present,
        "missing_ca_env": missing_ca_env,
        "agent_token_secret_ref_configured": bool(settings.onecli_agent_token_secret_ref),
        "node_proxy_bootstrap_configured": bool(node_proxy_bootstrap_path),
        "node_proxy_bootstrap_path": node_proxy_bootstrap_path,
        "direct_external_credentials_blocked": not forbidden_env_present,
        "forbidden_env_present": forbidden_env_present,
        "container_level_egress_controls": container_level_egress_controls,
        "worker_network": network_name,
        "direct_network_bypass_detection": "configured" if proxy_env_required else "not_enforced",
    }


def default_worker_codex_sandbox() -> str:
    configured = os.getenv("CODEX_CLI_SANDBOX", "").strip()
    if configured:
        return configured
    # Isolated workers often need write access for repo tasks, but they should
    # not silently escalate to full host access outside an explicit local-dev override.
    return "workspace-write"


class ExecutionControlPlane:
    def __init__(
            self,
            *,
            runtime_registry: RuntimeAdapterRegistry,
            execution_store,
            approval_manager: ApprovalManager,
            runtime_revision_service=None,
            runtime_container_manager=None,
            runtime_reconciler=None,
            runtime_operations=None,
            execution_isolation_enabled: Optional[bool] = None,
            cancel_outdated_executions: Optional[bool] = None,
            worker_id: Optional[str] = None,
            stale_after_seconds: int = 30,
    ):
        self.runtime_registry = runtime_registry
        self.execution_store = execution_store
        self.approval_manager = approval_manager
        self.runtime_revision_service = runtime_revision_service
        self.runtime_container_manager = runtime_container_manager
        self.runtime_reconciler = runtime_reconciler
        self.runtime_operations = runtime_operations
        settings = get_settings()
        self.execution_isolation_enabled = settings.execution_isolation_enabled if execution_isolation_enabled is None else execution_isolation_enabled
        self.runtime_revision_shadow_mode = settings.runtime_revision_shadow_mode
        self.cancel_outdated_executions = (
            settings.cancel_outdated_executions if cancel_outdated_executions is None else cancel_outdated_executions
        )
        self.worker_id = worker_id or os.getenv("EXECUTION_WORKER_ID") or f"worker-{uuid4()}"
        self.stale_after_seconds = stale_after_seconds
        self._tasks: dict[str, asyncio.Task] = {}
        self._container_watch_tasks: dict[str, asyncio.Task] = {}
        self.emitter = ExecutionEventEmitter(execution_store)
        self.lifecycle_emitter = RuntimeLifecycleEventEmitter(self.emitter)

    async def queue_start(self, execution_id: str):
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        _reject_terminal_execution_replay(execution)
        await self._reject_unresolved_wait(execution_id)
        execution.status = execution.status.__class__.QUEUED
        await self.execution_store.update_execution(execution)
        if execution_id not in self._tasks or self._tasks[execution_id].done():
            self._tasks[execution_id] = asyncio.create_task(self._run_execution(execution_id))
        return execution

    async def _run_execution(self, execution_id: str):
        acquired = await self.execution_store.acquire_lock(
            execution_id,
            self.worker_id,
            stale_after_seconds=self.stale_after_seconds,
        )
        if not acquired:
            return
        try:
            execution = await self.execution_store.get_execution(execution_id)
            if execution is None:
                return
            execution_host = self._execution_host_for(execution)
            if execution_host == EXECUTION_HOST_DOCKER:
                prepared = await self._prepare_isolated_runtime(execution)
                if prepared is None:
                    return
                return
            elif self.runtime_revision_shadow_mode:
                execution = await self._prepare_shadow_runtime(execution)
            execution.status = execution.status.__class__.RUNNING
            execution.worker_id = self.worker_id
            execution.last_heartbeat_at = utc_now()
            await self.execution_store.update_execution(execution)
            stop_heartbeat = asyncio.Event()
            heartbeat_task = asyncio.create_task(
                self._heartbeat_while_local_execution_runs(
                    execution_id=execution_id,
                    worker_id=self.worker_id,
                    stop_signal=stop_heartbeat,
                )
            )
            try:
                await self.runtime_registry.start_execution(execution_id)
            finally:
                stop_heartbeat.set()
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
        finally:
            await self.execution_store.release_lock(execution_id, self.worker_id)
            self._tasks.pop(execution_id, None)

    async def _heartbeat_while_local_execution_runs(
            self,
            *,
            execution_id: str,
            worker_id: str,
            stop_signal: asyncio.Event,
    ) -> None:
        # Local runs execute in-process, so a long model/tool call can otherwise leave
        # the run looking dead even though the event loop is still healthy.
        interval_seconds = max(1.0, float(self.stale_after_seconds) / 3.0)
        while not stop_signal.is_set():
            await asyncio.sleep(interval_seconds)
            if stop_signal.is_set():
                break
            await self.execution_store.heartbeat(execution_id, worker_id)

    def _execution_host_for(self, execution) -> str:
        if self.execution_isolation_enabled:
            # Isolation is an operator security boundary, not a per-execution
            # preference. Request/workflow metadata may request Docker when the
            # default is local, but can never downgrade an enabled boundary.
            return EXECUTION_HOST_DOCKER
        metadata = execution.metadata if isinstance(execution.metadata, dict) else {}
        requested_host = metadata.get("execution_host")
        if isinstance(requested_host, str):
            normalized_host = requested_host.strip().lower()
            if normalized_host in {EXECUTION_HOST_LOCAL, EXECUTION_HOST_DOCKER}:
                return normalized_host
        return EXECUTION_HOST_LOCAL

    async def pause(self, execution_id: str):
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        if execution.status == ExecutionStatus.SLEEPING:
            await self._close_pending_waits(
                execution,
                reason="operator_pause",
                kinds={ExecutionWaitKind.SLEEP},
                set_paused=True,
            )
        return await self.runtime_registry.pause_execution(execution_id)

    async def resume(self, execution_id: str):
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        _reject_terminal_execution_replay(execution)
        await self._reject_unresolved_wait(execution_id)
        execution = await self.runtime_registry.resume_execution(execution_id)
        if execution.status.value in {"running", "queued"}:
            await self.queue_start(execution_id)
        return execution

    async def _reject_unresolved_wait(self, execution_id: str) -> None:
        if not hasattr(self.execution_store, "list_execution_waits"):
            return
        pending = await self.execution_store.list_execution_waits(
            execution_id,
            status=ExecutionWaitStatus.PENDING,
        )
        if pending:
            wait = pending[0]
            # Wake resolution atomically claims the continuation before it
            # reaches this control-plane boundary. Generic lifecycle commands
            # must not create a second path around that claim.
            raise ValueError(
                f"Execution '{execution_id}' has unresolved {wait.kind.value} wait '{wait.id}'. "
                "Resolve the wait before starting or resuming the execution."
            )

    async def cancel(self, execution_id: str):
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        if execution.status.value in TERMINAL_EXECUTION_STATUSES or execution.status.value == "cancelling":
            return execution
        await self._close_pending_waits(execution, reason="operator_cancel")
        execution = await self.execution_store.get_execution(execution_id) or execution
        preservation = await self._partial_result_preservation_snapshot(execution, reason="cancel_requested")
        if preservation:
            metadata = dict(execution.metadata or {})
            metadata["partial_result_preservation"] = preservation
            execution.metadata = metadata
        execution.status = execution.status.__class__.CANCELLING
        await self.execution_store.update_execution(execution)
        cancelled = await self.runtime_registry.cancel_execution(execution_id)
        await self._close_browser_sessions(execution_id)
        if preservation:
            metadata = dict(cancelled.metadata or {})
            metadata["partial_result_preservation"] = preservation
            cancelled.metadata = metadata
            cancelled = await self.execution_store.update_execution(cancelled)
        return cancelled

    async def _close_browser_sessions(self, execution_id: str) -> None:
        master_secret = os.getenv("BROWSER_RUNTIME_SIGNING_SECRET")
        if not master_secret:
            return
        try:
            from app.browser_runtime.client import BrowserRuntimeClient

            client = BrowserRuntimeClient(signing_secret=master_secret)
            try:
                await asyncio.to_thread(
                    client.close_execution,
                    execution_id,
                    owner={"execution_id": execution_id},
                )
            finally:
                client.close_client()
        except Exception:
            # Cancellation remains authoritative even when the auxiliary
            # runtime is unavailable; its TTL reaper is the final safeguard.
            return

    async def _close_pending_waits(
            self,
            execution,
            *,
            reason: str,
            kinds: set[ExecutionWaitKind] | None = None,
            set_paused: bool = False,
    ) -> None:
        if not hasattr(self.execution_store, "list_execution_waits"):
            return
        pending = await self.execution_store.list_execution_waits(
            execution.id,
            status=ExecutionWaitStatus.PENDING,
        )
        closed = []
        for wait in pending:
            if kinds is not None and wait.kind not in kinds:
                continue
            resolved, claimed = await self.execution_store.resolve_execution_wait(
                wait.id,
                status=ExecutionWaitStatus.CANCELLED,
                resolution_key=f"{reason}:{wait.id}"[:255],
                resolution_payload={"reason": reason},
                resolved_by="execution_control_plane",
            )
            if claimed and resolved is not None:
                closed.append(resolved)
        if not closed:
            return

        metadata = dict(execution.metadata or {})
        active_wait = metadata.get("active_wait")
        closed_ids = {wait.id for wait in closed}
        if isinstance(active_wait, dict) and active_wait.get("wait_id") in closed_ids:
            metadata.pop("active_wait", None)
        metadata["last_resolved_wait"] = {
            "wait_id": closed[-1].id,
            "kind": closed[-1].kind.value,
            "status": closed[-1].status.value,
            "resolution_key": closed[-1].resolution_key,
            "resolved_at": closed[-1].resolved_at.isoformat() if closed[-1].resolved_at else None,
        }
        execution.metadata = metadata
        if set_paused:
            execution.status = ExecutionStatus.PAUSED
        await self.execution_store.update_execution(execution)

    async def approve(self, execution_id: str, tool_id: str, reason: str | None = None) -> bool:
        approved = await self.approval_manager.approve(
            execution_id=execution_id,
            tool_id=tool_id,
            reason=reason,
        )
        if approved:
            await self._wake_after_approval_decision(execution_id)
        return approved

    async def reject(self, execution_id: str, tool_id: str, reason: str | None = None) -> bool:
        rejected = await self.approval_manager.reject(
            execution_id=execution_id,
            tool_id=tool_id,
            reason=reason,
        )
        if rejected:
            await self._wake_after_approval_decision(execution_id)
        return rejected

    async def _wake_after_approval_decision(self, execution_id: str) -> None:
        """Claim the durable continuation after the approval row is resolved."""
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            return
        active_wait = (execution.metadata or {}).get("active_wait")
        wait_id = active_wait.get("wait_id") if isinstance(active_wait, dict) else None
        if not isinstance(wait_id, str) or not hasattr(self.execution_store, "get_execution_wait"):
            return
        wait = await self.execution_store.get_execution_wait(wait_id)
        if wait is None or wait.kind != ExecutionWaitKind.APPROVAL:
            return
        if wait.status == ExecutionWaitStatus.PENDING:
            return

        # A very fast approval can arrive before the suspending coroutine has
        # released its lock. Wait for that local owner before queueing resume.
        owner_task = self._tasks.get(execution_id)
        if owner_task is not None and owner_task is not asyncio.current_task() and not owner_task.done():
            await owner_task

        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            return
        metadata = dict(execution.metadata or {})
        active_wait = metadata.get("active_wait")
        if not isinstance(active_wait, dict) or active_wait.get("wait_id") != wait_id:
            return
        metadata.pop("active_wait", None)
        metadata["last_resolved_wait"] = {
            "wait_id": wait.id,
            "kind": wait.kind.value,
            "status": wait.status.value,
            "resolution_key": wait.resolution_key,
            "resolved_at": wait.resolved_at.isoformat() if wait.resolved_at else None,
        }
        execution.metadata = metadata
        wait_resolutions = dict((execution.input_payload or {}).get("wait_resolutions") or {})
        wait_resolutions[wait.id] = {
            "kind": wait.kind.value,
            "status": wait.status.value,
            "payload": dict(wait.resolution_payload or {}),
            "resolved_at": wait.resolved_at.isoformat() if wait.resolved_at else None,
        }
        execution.input_payload = {**(execution.input_payload or {}), "wait_resolutions": wait_resolutions}
        execution.status = ExecutionStatus.PAUSED
        await self.execution_store.update_execution(execution)

        state = NativeExecutionState(execution_id=execution.id, workflow_id=execution.workflow_id)
        events = await self.execution_store.list_events(execution.id)
        if events:
            state.sequence = events[-1].sequence
            state.last_event_id = events[-1].id
            state.trace_id = events[-1].trace_id or state.trace_id
        await self.emitter.emit(
            state,
            ExecutionEventType.EXECUTION_WOKEN,
            payload={
                "wait_id": wait.id,
                "kind": wait.kind.value,
                "status": wait.status.value,
                "resolution_key": wait.resolution_key,
                "resume_requested": True,
            },
        )
        await self._wait_for_approval_worker_release(execution.id)
        await self.resume(execution.id)

    async def _wait_for_approval_worker_release(self, execution_id: str) -> None:
        """Wait until the suspending worker releases its lock or becomes stale."""
        deadline = asyncio.get_running_loop().time() + max(1.0, float(self.stale_after_seconds) + 1.0)
        while asyncio.get_running_loop().time() < deadline:
            execution = await self.execution_store.get_execution(execution_id)
            if execution is None or execution.worker_id is None:
                return
            heartbeat = execution.last_heartbeat_at
            if heartbeat is None or (utc_now() - heartbeat).total_seconds() > self.stale_after_seconds:
                # queue_start can safely take over a stale lock using the same
                # ownership rule as ordinary execution recovery.
                return
            await asyncio.sleep(0.05)

    async def heartbeat(self, execution_id: str):
        return await self.execution_store.heartbeat(execution_id, self.worker_id)

    async def recover_stale_executions(self, *, workflow_id: str | None = None):
        repaired = await self.repair_stale_executions(workflow_id=workflow_id)
        return [item["execution_id"] for item in repaired]

    async def repair_stale_executions(
            self,
            *,
            workflow_id: str | None = None,
            execution_id: str | None = None,
    ) -> list[dict[str, object]]:
        recovered = []
        active = await self.execution_store.list_active_executions()
        for execution in active:
            if execution_id is not None and execution.id != execution_id:
                continue
            if workflow_id is not None and execution.workflow_id != workflow_id:
                continue
            if execution.status.value not in STALE_REPAIR_STATUSES:
                continue
            classification = self._classify_stale_execution(execution)
            if not classification["is_stale"]:
                continue
            previous_status = execution.status.value
            completed_checkpoint = await self._has_completed_workflow_checkpoint(execution)
            if previous_status == "waiting_for_approval":
                # The suspended Python continuation cannot be reconstructed after
                # its worker dies. Failing is safer than requeueing and possibly
                # repeating side effects that occurred before the approval point.
                repair_action = "failed_abandoned_approval"
                execution.status = execution.status.__class__.FAILED
                execution.completed_at = execution.completed_at or utc_now()
                execution.error = (
                    "Approval wait was abandoned because the execution worker stopped heartbeating. "
                    "Resume from a safe checkpoint or start a replacement execution."
                )
                event_type = ExecutionEventType.EXECUTION_FAILED
            elif completed_checkpoint and previous_status != "cancelling":
                repair_action = "marked_completed"
                execution.status = execution.status.__class__.COMPLETED
                execution.completed_at = execution.completed_at or utc_now()
                execution.error = None
                event_type = ExecutionEventType.EXECUTION_COMPLETED
            elif previous_status == "cancelling":
                repair_action = "marked_cancelled"
                execution.status = execution.status.__class__.CANCELLED
                execution.completed_at = execution.completed_at or utc_now()
                execution.error = execution.error or "Stale cancelling execution was marked cancelled"
                event_type = ExecutionEventType.EXECUTION_CANCELLED
            else:
                repair_action = "requeued"
                execution.status = execution.status.__class__.QUEUED
                event_type = ExecutionEventType.EXECUTION_REPAIRED
            preservation = None
            if repair_action != "marked_completed":
                preservation = await self._partial_result_preservation_snapshot(
                    execution,
                    reason=f"stale_execution_{repair_action}",
                )
            state = await self._event_state_for(execution.id, execution.workflow_id)
            container_repair = None
            if repair_action in {"requeued", "marked_completed", "failed_abandoned_approval"}:
                container_repair = await self._cleanup_stale_execution_container(
                    execution,
                    state=state,
                    reason=f"stale_execution_{repair_action}",
                )
            expired_approvals = []
            if repair_action == "failed_abandoned_approval":
                expired_approvals = await self._expire_pending_execution_approvals(
                    execution,
                    reason=execution.error or "Approval worker stopped heartbeating.",
                )
            execution.worker_id = None
            execution.last_heartbeat_at = None
            metadata = dict(execution.metadata or {})
            if repair_action == "failed_abandoned_approval":
                metadata.pop("pending_approval", None)
            if preservation:
                metadata["partial_result_preservation"] = preservation
            stale_repair = {
                "repaired_at": utc_now().isoformat(),
                "previous_status": previous_status,
                "repair_action": repair_action,
                "stale_after_seconds": self.stale_after_seconds,
                "age_seconds": classification["age_seconds"],
            }
            if container_repair:
                stale_repair["container_repair"] = container_repair
            if expired_approvals:
                stale_repair["expired_approval_request_ids"] = expired_approvals
            metadata["stale_repair"] = stale_repair
            execution.metadata = metadata
            execution.updated_at = utc_now()
            await self.execution_store.update_execution(execution)
            await self.emitter.emit(
                state,
                event_type,
                payload={
                    "execution_id": execution.id,
                    "previous_status": previous_status,
                    "new_status": execution.status.value,
                    "repair_action": repair_action,
                    "reason": classification["reason"],
                    "stale_classification": classification,
                    **({"output": execution.output_payload} if repair_action == "marked_completed" else {}),
                    **({"partial_result_preservation": preservation} if preservation else {}),
                    **({"container_repair": container_repair} if container_repair else {}),
                    **({"expired_approval_request_ids": expired_approvals} if expired_approvals else {}),
                },
            )
            if self.runtime_operations is not None:
                self.runtime_operations.increment("stale_execution_repairs")
                self.runtime_operations.increment(f"stale_execution_repairs.{repair_action}")
                self.runtime_operations.increment(f"stale_execution_repairs.status.{previous_status}")
                self.runtime_operations.record_action(
                    "stale_execution_repair",
                    execution_id=execution.id,
                    previous_status=previous_status,
                    new_status=execution.status.value,
                    repair_action=repair_action,
                    reason=classification["reason"],
                )
            recovered.append(
                {
                    "execution_id": execution.id,
                    "previous_status": previous_status,
                    "new_status": execution.status.value,
                    "repair_action": repair_action,
                    "reason": classification["reason"],
                }
            )
        return recovered

    async def _expire_pending_execution_approvals(self, execution, *, reason: str) -> list[str]:
        """Make abandoned approval rows non-actionable when their owning continuation is gone."""
        if not all(
                hasattr(self.execution_store, method)
                for method in ("list_approval_requests", "resolve_pending_approval_request")
        ):
            return []
        requests = await self.execution_store.list_approval_requests(execution.id)
        expired_ids: list[str] = []
        for request in requests:
            if request.get("status") != "pending" or not request.get("tool_id"):
                continue
            expired = await self.execution_store.resolve_pending_approval_request(
                execution_id=execution.id,
                tool_id=str(request["tool_id"]),
                status="expired",
                response_payload={
                    "granted": False,
                    "reason": reason,
                    "metadata": {"mode": "runtime_reconciler", "cause": "worker_unresponsive"},
                },
                responded_by="runtime_reconciler",
            )
            if expired is not None:
                expired_ids.append(str(expired["id"]))
        return expired_ids

    async def _has_completed_workflow_checkpoint(self, execution) -> bool:
        output_payload = execution.output_payload
        if not isinstance(output_payload, dict) or "final_output" not in output_payload:
            return False
        checkpoint = output_payload.get("checkpoint")
        if not isinstance(checkpoint, dict):
            return False
        completed_node_ids = {
            item
            for item in checkpoint.get("completed_node_ids", [])
            if isinstance(item, str) and item
        }
        if not completed_node_ids:
            return False
        checkpoint_terminal_node_ids = {
            item
            for item in checkpoint.get("terminal_node_ids", [])
            if isinstance(item, str) and item
        }
        if checkpoint_terminal_node_ids:
            return checkpoint_terminal_node_ids.issubset(completed_node_ids)
        workflow = await self.runtime_registry.workflow_repository.get_workflow(execution.workflow_id)
        if workflow is None or not workflow.nodes:
            return False
        task_node_ids = {
            node.id
            for node in workflow.nodes
            if node.node_type == "task"
        }
        outgoing: dict[str, list[str]] = {}
        for edge in workflow.edges:
            outgoing.setdefault(edge.source_node_id, []).append(edge.target_node_id)
        terminal_node_ids = set()
        for node_id in task_node_ids:
            pending = list(outgoing.get(node_id, []))
            seen: set[str] = set()
            reaches_later_task = False
            while pending:
                target_id = pending.pop()
                if target_id in seen:
                    continue
                seen.add(target_id)
                if target_id in task_node_ids:
                    reaches_later_task = True
                    break
                pending.extend(outgoing.get(target_id, []))
            if not reaches_later_task:
                terminal_node_ids.add(node_id)
        if not terminal_node_ids:
            return False
        # A stale worker can die after the final node side effect has persisted
        # but before the engine flips the execution row to completed. In that
        # case, requeueing would repeat external effects such as notifications.
        return terminal_node_ids.issubset(completed_node_ids)

    def _classify_stale_execution(self, execution) -> dict[str, object]:
        # Repair and supervision must agree about intentional waits. A separate
        # timestamp-only check can otherwise restart paused work or consume a
        # durable continuation before its wake condition is satisfied.
        return classify_execution_staleness(
            execution,
            stale_after_seconds=self.stale_after_seconds,
        )

    async def _cleanup_stale_execution_container(
            self,
            execution,
            *,
            state: NativeExecutionState,
            reason: str,
    ) -> dict[str, object] | None:
        if not execution.container_id or self.runtime_container_manager is None:
            return None
        result: dict[str, object] = {
            "container_id": execution.container_id,
            "previous_container_status": execution.container_status,
            "action": "none",
        }
        try:
            container = self.runtime_container_manager.inspect_container(execution.container_id)
            result["inspected_status"] = container.status
            if container.status in LIVE_CONTAINER_STATUSES:
                container = self.runtime_container_manager.stop_container(execution.container_id)
                result["action"] = "stopped_and_removed"
            elif container.status in EXITED_CONTAINER_STATUSES:
                result["action"] = "removed"
            else:
                result["action"] = "removed"
            self.runtime_container_manager.remove_container(
                execution.container_id,
                force=container.status in EXITED_CONTAINER_STATUSES,
            )
            execution.container_status = "removed"
            execution.container_ended_at = container.finished_at or execution.container_ended_at or utc_now()
            execution.container_exit_code = container.exit_code
            await self.lifecycle_emitter.emit_container_stopped(
                state,
                container,
                runtime_revision_id=execution.runtime_revision_id,
                reason=reason,
            )
            result.update(
                {
                    "new_container_status": execution.container_status,
                    "exit_code": container.exit_code,
                }
            )
        except Exception as exc:
            result.update({"action": "cleanup_failed", "error": str(exc)})
        return result

    async def _prepare_isolated_runtime(self, execution):
        if self.runtime_revision_service is None or self.runtime_container_manager is None:
            raise RuntimeError("Execution isolation requires runtime revision and container manager services")
        if execution.runtime_adapter_id != "native":
            raise ContainerRuntimeError("Only the native runtime adapter is supported for isolated execution hosting")

        state = await self._event_state_for(execution.id, execution.workflow_id)
        settings = get_settings()
        try:
            revision = await self.runtime_revision_service.resolve_current_revision(
                metadata={
                    "execution_id": execution.id,
                    "workflow_id": execution.workflow_id,
                    "requested_adapter": execution.runtime_adapter_id,
                    "onecli": onecli_runtime_metadata(settings),
                }
            )
            execution.runtime_revision_id = revision.id
            execution.runtime_fingerprint = revision.fingerprint
            await self.execution_store.update_execution(execution)
            await self.lifecycle_emitter.emit_runtime_revision_resolved(state, revision)
            invalidated = await self.runtime_revision_service.invalidate_superseded_revisions(
                revision.id,
                reason=f"superseded_by:{revision.id}",
            )
            for invalidated_revision in invalidated:
                await self.lifecycle_emitter.emit_runtime_revision_invalidated(
                    state,
                    invalidated_revision,
                    reason=invalidated_revision.invalidation_reason,
                )
            execution = await self._handle_outdated_executions(execution, revision.id)
            workflow = await self.runtime_registry.workflow_repository.get_workflow(execution.workflow_id)
            runtime_policy = resolve_execution_runtime_policy(
                settings=settings,
                workflow=workflow,
                execution=execution,
                include_workflow_member_maxima=True,
            )
            execution.metadata = {
                **(execution.metadata if isinstance(execution.metadata, dict) else {}),
                "runtime_policy": runtime_policy.model_dump(),
            }
            await self.execution_store.update_execution(execution)

            image = (
                f"{revision.image_name}:{revision.image_tag}"
                if revision.image_name and revision.image_tag
                else self.runtime_container_manager.config.runtime_base_image
            )
            worker_codex_cwd = os.getenv("EXECUTION_CODEX_CLI_CWD")
            if not worker_codex_cwd:
                if os.getenv("AGENCY_BACKEND_RUN_MODE") == "host":
                    worker_codex_cwd = settings.execution_container_workdir
                else:
                    worker_codex_cwd = os.getenv("CODEX_CLI_CWD", settings.execution_container_workdir)
            container_env = {
                "AGENCY_EXECUTION_ID": execution.id,
                "AGENCY_WORKFLOW_ID": execution.workflow_id,
                "AGENCY_RUNTIME_REVISION_ID": revision.id,
                "AGENCY_RUNTIME_ADAPTER_ID": execution.runtime_adapter_id,
                "AGENCY_WORKER_ID": f"container-worker-{execution.id}",
                "AGENCY_HEARTBEAT_INTERVAL_SECONDS": str(runtime_policy.heartbeat_interval_seconds),
                "APP_ENV": settings.app_env,
                "CODEX_HOME": os.getenv("EXECUTION_CODEX_HOME") or "/codex",
                "CODEX_CLI_CWD": worker_codex_cwd,
                "CODEX_CLI_SANDBOX": default_worker_codex_sandbox(),
                "CODEX_CLI_TIMEOUT_SECONDS": str(runtime_policy.codex_cli_timeout_seconds),
                "LLM_REQUEST_TIMEOUT_SECONDS": f"{runtime_policy.llm_request_timeout_seconds:g}",
                **onecli_worker_environment(settings),
                **({"DATABASE_URL": settings.container_database_url} if settings.container_database_url else {}),
            }
            browser_runtime_master_secret = os.getenv("BROWSER_RUNTIME_SIGNING_SECRET")
            if browser_runtime_master_secret:
                from app.browser_runtime.security import derive_execution_secret

                # Workers receive only an execution-derived key. It cannot mint
                # a valid capability for another execution or actor scope.
                container_env.update({
                    "BROWSER_RUNTIME_URL": os.getenv("BROWSER_RUNTIME_WORKER_URL", "http://browser-runtime:8010"),
                    "BROWSER_RUNTIME_EXECUTION_SECRET": derive_execution_secret(
                        browser_runtime_master_secret,
                        execution.id,
                    ),
                })
            if runtime_policy.worker_hard_timeout_seconds is not None:
                container_env["AGENCY_EXECUTION_TIMEOUT_SECONDS"] = str(runtime_policy.worker_hard_timeout_seconds)
            if execution.goal_id:
                container_env["AGENCY_GOAL_ID"] = execution.goal_id
            worker_network_name = onecli_worker_network_name(settings)
            onecli_diagnostics = onecli_worker_enforcement_diagnostics(
                settings,
                container_env,
                network_name=worker_network_name,
            )
            execution.metadata = {
                **(execution.metadata if isinstance(execution.metadata, dict) else {}),
                "worker_context": {
                    "execution_id": execution.id,
                    "workflow_id": execution.workflow_id,
                    "goal_id": execution.goal_id,
                    "runtime_revision_id": revision.id,
                    "runtime_adapter_id": execution.runtime_adapter_id,
                    "worker_id": container_env["AGENCY_WORKER_ID"],
                },
                "onecli_worker_enforcement": onecli_diagnostics,
            }
            await self.execution_store.update_execution(execution)
            await self.emitter.emit(
                state,
                ExecutionEventType.ONECLI_WORKER_ENFORCEMENT_RECORDED,
                payload=onecli_diagnostics,
            )
            container_spec = RuntimeContainerSpec(
                execution_id=execution.id,
                workflow_id=execution.workflow_id,
                runtime_revision_id=revision.id,
                image=image,
                goal_id=execution.goal_id,
                command=["python", "-m", "app.runtime.worker"],
                env=container_env,
                labels={
                    "agency.onecli.enabled": str(settings.onecli_enabled).lower(),
                    "agency.onecli.isolated_workers": str(settings.onecli_force_for_isolated_workers).lower(),
                    "agency.onecli.egress_mode": settings.onecli_worker_egress_mode,
                },
                network_name=worker_network_name,
            )
            created = self.runtime_container_manager.create_execution_container(container_spec)
            execution.container_id = created.container_id
            execution.container_name = created.name
            execution.container_image = created.image
            execution.container_status = created.status
            await self.execution_store.update_execution(execution)
            await self.lifecycle_emitter.emit_container_created(
                state,
                created,
                runtime_revision_id=revision.id,
            )

            started = self.runtime_container_manager.start_container(created.container_id)
            execution.container_status = started.status
            execution.container_started_at = started.started_at or utc_now()
            execution.container_ended_at = started.finished_at
            execution.container_exit_code = started.exit_code
            await self.execution_store.update_execution(execution)
            await self.lifecycle_emitter.emit_container_started(
                state,
                started,
                runtime_revision_id=revision.id,
            )
            execution.container_status = started.status
            await self.execution_store.update_execution(execution)
            self._start_container_watch(execution.id, started.container_id)
            return execution
        except ContainerRuntimeError as exc:
            execution.status = execution.status.__class__.FAILED
            execution.error = str(exc)
            execution.completed_at = utc_now()
            await self.execution_store.update_execution(execution)
            failed_container = RuntimeContainerState(
                container_id=execution.container_id or "unknown",
                name=execution.container_name or "unknown",
                image=execution.container_image or "unknown",
                status=execution.container_status or "failed",
                labels={},
                started_at=execution.container_started_at,
                finished_at=execution.container_ended_at,
                exit_code=execution.container_exit_code,
            )
            await self.lifecycle_emitter.emit_container_failed(
                state,
                failed_container,
                runtime_revision_id=execution.runtime_revision_id,
                reason=str(exc),
                extra={"error": str(exc)},
            )
            await self.emitter.emit(
                state,
                ExecutionEventType.EXECUTION_FAILED,
                payload={"error": str(exc)},
            )
            return None

    def _start_container_watch(self, execution_id: str, container_id: str) -> None:
        existing = self._container_watch_tasks.get(execution_id)
        if existing is not None and not existing.done():
            existing.cancel()
        self._container_watch_tasks[execution_id] = asyncio.create_task(
            self._watch_container_until_exit(execution_id, container_id)
        )

    async def _watch_container_until_exit(self, execution_id: str, container_id: str) -> None:
        if self.runtime_container_manager is None or self.runtime_reconciler is None:
            return
        try:
            state = await asyncio.to_thread(
                self.runtime_container_manager.wait_for_container_exit,
                container_id,
                timeout_seconds=max(get_settings().runtime_container_ttl_seconds, 60),
                poll_interval_seconds=1.0,
            )
            if self.runtime_operations is not None:
                self.runtime_operations.increment("container_watch.completed")
                self.runtime_operations.record_action(
                    "container_watch_exit",
                    execution_id=execution_id,
                    container_id=container_id,
                    status=state.status,
                    exit_code=state.exit_code,
                )
            await self.runtime_reconciler.reconcile_execution(execution_id, known_container=state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.runtime_operations is not None:
                self.runtime_operations.increment("container_watch.failed")
                self.runtime_operations.record_action(
                    "container_watch_failed",
                    execution_id=execution_id,
                    container_id=container_id,
                    detail=str(exc),
                )
        finally:
            self._container_watch_tasks.pop(execution_id, None)

    async def _prepare_shadow_runtime(self, execution):
        if self.runtime_revision_service is None:
            raise RuntimeError("Runtime revision shadow mode requires a runtime revision service")

        state = await self._event_state_for(execution.id, execution.workflow_id)
        revision = await self.runtime_revision_service.resolve_current_revision(
            metadata={
                "execution_id": execution.id,
                "workflow_id": execution.workflow_id,
                "requested_adapter": execution.runtime_adapter_id,
                "shadow_mode": True,
            }
        )
        execution.runtime_revision_id = revision.id
        execution.runtime_fingerprint = revision.fingerprint
        await self.execution_store.update_execution(execution)
        await self.lifecycle_emitter.emit_runtime_revision_resolved(state, revision)
        return execution

    async def _handle_outdated_executions(self, execution, runtime_revision_id: str):
        if execution.replacement_of_execution_id:
            return execution
        active = await self.execution_store.list_active_executions()
        outdated = [
            candidate
            for candidate in active
            if candidate.id != execution.id
               and candidate.workflow_id == execution.workflow_id
               and candidate.status.value in ACTIVE_REPLACEMENT_STATUSES
               and candidate.runtime_revision_id
               and candidate.runtime_revision_id != runtime_revision_id
        ]
        if not outdated:
            return execution

        replacement_target = outdated[0]
        changed = False
        if execution.replacement_of_execution_id != replacement_target.id:
            execution.replacement_of_execution_id = replacement_target.id
            changed = True
        if execution.restart_reason != "runtime_revision_superseded":
            execution.restart_reason = "runtime_revision_superseded"
            changed = True
        if changed:
            execution = await self.execution_store.update_execution(execution)

        if not self.cancel_outdated_executions:
            return execution

        for candidate in outdated:
            await self._replace_outdated_execution(
                candidate,
                replacement_execution=execution,
                runtime_revision_id=runtime_revision_id,
                reason="runtime_revision_superseded",
            )
        return execution

    async def replace_active_executions_for_workflow_revision(
            self,
            *,
            workflow_id: str,
            previous_revision: int | None,
            replacement_revision: int,
            source: str = "workflow_revision_change",
    ) -> list[str]:
        active = await self.execution_store.list_active_executions()
        replaced: list[str] = []
        candidates = [
            execution
            for execution in active
            if execution.workflow_id == workflow_id
               and execution.status.value in ACTIVE_REPLACEMENT_STATUSES
               and execution.restart_reason != "workflow_revision_superseded"
        ]
        for execution in candidates:
            replacement = await self.runtime_registry.create_execution(
                execution.workflow_id,
                execution.input_payload,
                {
                    "type": "workflow_revision_superseded",
                    "source": source,
                    "created_by": "workflow-update",
                    "replaces_execution_id": execution.id,
                    "previous_workflow_revision": previous_revision,
                    "replacement_workflow_revision": replacement_revision,
                },
                runtime_adapter_id=execution.runtime_adapter_id,
                goal_id=execution.goal_id,
            )
            replacement.replacement_of_execution_id = execution.id
            replacement.restart_reason = "workflow_revision_superseded"
            preservation = await self._partial_result_preservation_snapshot(
                execution,
                reason="workflow_revision_superseded",
            )
            if preservation:
                replacement.metadata = {
                    **(replacement.metadata or {}),
                    "source_partial_result_preservation": preservation,
                }
            await self.execution_store.update_execution(replacement)
            await self.queue_start(replacement.id)
            await self._replace_outdated_execution(
                execution,
                replacement_execution=replacement,
                runtime_revision_id=execution.runtime_revision_id or replacement.runtime_revision_id or "",
                reason="workflow_revision_superseded",
                preservation=preservation,
                extra={
                    "replacement_workflow_revision": replacement_revision,
                    "previous_workflow_revision": previous_revision,
                },
            )
            replaced.append(replacement.id)
        return replaced

    async def _replace_outdated_execution(
            self,
            execution,
            *,
            replacement_execution,
            runtime_revision_id: str,
            reason: str,
            preservation: dict[str, Any] | None = None,
            extra: dict | None = None,
    ) -> None:
        state = await self._event_state_for(execution.id, execution.workflow_id)
        preservation = preservation or await self._partial_result_preservation_snapshot(execution, reason=reason)
        execution.restart_reason = reason
        if preservation:
            execution.metadata = {
                **(execution.metadata or {}),
                "partial_result_preservation": preservation,
            }
        try:
            execution = await self.runtime_registry.cancel_execution(execution.id)
        except Exception:
            execution.status = execution.status.__class__.CANCELLED
            execution.completed_at = execution.completed_at or utc_now()
            if preservation:
                execution.metadata = {
                    **(execution.metadata or {}),
                    "partial_result_preservation": preservation,
                }
            await self.execution_store.update_execution(execution)
            await self.emitter.emit(
                state,
                ExecutionEventType.EXECUTION_CANCELLED,
                payload={
                    "execution_id": execution.id,
                    "reason": reason,
                    "replacement_execution_id": replacement_execution.id,
                    **({"partial_result_preservation": preservation} if preservation else {}),
                },
            )
        else:
            if preservation:
                execution.metadata = {
                    **(execution.metadata or {}),
                    "partial_result_preservation": preservation,
                }
                await self.execution_store.update_execution(execution)

        execution.restart_reason = reason
        if not execution.container_id:
            await self.execution_store.update_execution(execution)
            return
        container = self.runtime_container_manager.inspect_container(execution.container_id)
        if container.status in LIVE_CONTAINER_STATUSES:
            container = self.runtime_container_manager.stop_container(execution.container_id)
        self.runtime_container_manager.remove_container(
            execution.container_id,
            force=container.status in EXITED_CONTAINER_STATUSES,
        )
        finalized_cancellation = False
        # Replacement owns the isolated worker shutdown, so finalize cooperative
        # cancellation here instead of waiting for a worker that was just removed.
        if execution.status.value == "cancelling":
            execution.status = execution.status.__class__.CANCELLED
            execution.completed_at = execution.completed_at or utc_now()
            finalized_cancellation = True
        execution.container_status = "removed"
        execution.container_ended_at = container.finished_at or execution.container_ended_at or utc_now()
        execution.container_exit_code = container.exit_code
        await self.execution_store.update_execution(execution)
        if finalized_cancellation:
            await self.emitter.emit(
                state,
                ExecutionEventType.EXECUTION_CANCELLED,
                payload={
                    "execution_id": execution.id,
                    "reason": reason,
                    "replacement_execution_id": replacement_execution.id,
                    **({"partial_result_preservation": preservation} if preservation else {}),
                },
            )
        await self.lifecycle_emitter.emit_container_replaced(
            state,
            container,
            runtime_revision_id=execution.runtime_revision_id,
            reason=reason,
            extra={
                "replacement_execution_id": replacement_execution.id,
                "replacement_runtime_revision_id": runtime_revision_id,
                **({"partial_result_preservation": preservation} if preservation else {}),
                **(extra or {}),
            },
        )

    async def _partial_result_preservation_snapshot(self, execution, *, reason: str) -> dict[str, Any] | None:
        artifacts = await self.execution_store.list_artifacts(execution.id)
        output_payload = execution.output_payload if isinstance(execution.output_payload, dict) else None
        node_outputs = output_payload.get("node_outputs") if output_payload is not None else {}
        if not isinstance(node_outputs, dict):
            node_outputs = {}
        has_output = execution.output_payload is not None
        if not has_output and not artifacts:
            return None
        snapshot: dict[str, Any] = {
            "recorded_at": utc_now().isoformat(),
            "reason": reason,
            "execution_id": execution.id,
            "workflow_id": execution.workflow_id,
            "goal_id": execution.goal_id,
            "output_payload_present": has_output,
            "output_payload_keys": sorted(output_payload.keys()) if output_payload is not None else [],
            "node_output_ids": sorted(str(node_id) for node_id in node_outputs),
            "artifact_count": len(artifacts),
            "artifacts": [
                {
                    "artifact_id": artifact.id,
                    "event_id": artifact.event_id,
                    "name": artifact.name,
                    "artifact_type": artifact.artifact_type,
                    "uri": artifact.uri,
                    "media_type": artifact.media_type,
                    "size_bytes": artifact.size_bytes,
                }
                for artifact in artifacts
            ],
        }
        return snapshot

    async def _event_state_for(self, execution_id: str, workflow_id: str) -> NativeExecutionState:
        existing_events = await self.execution_store.list_events(execution_id)
        state = NativeExecutionState(execution_id=execution_id, workflow_id=workflow_id)
        if existing_events:
            last_event = existing_events[-1]
            state.sequence = last_event.sequence
            state.last_event_id = last_event.id
            state.trace_id = last_event.trace_id or state.trace_id
        return state
