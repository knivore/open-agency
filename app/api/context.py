"""Application dependency graph for API, runtime, scheduler, and tests.

`ApiContext` is the backend composition root. Route factories and services receive
one context instead of constructing repositories, registries, runtime adapters, or
tool catalogs themselves. The helpers at the bottom build production, worker, and
test contexts while preserving the same service wiring shape.
"""

from __future__ import annotations

import importlib
import os
import shlex
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.conversation_streaming import ConversationEventBroker
from app.core.config import get_settings
from app.db.repositories import (
    InMemoryCatalogRepository,
    InMemoryApiTokenRepository,
    InMemoryConversationApprovalRequestRepository,
    InMemoryConversationMessageRepository,
    InMemoryConversationRepository,
    InMemoryGraphProjectionEventRepository,
    InMemoryMemoryRepository,
    InMemoryModelProfileCatalogRepository,
    InMemoryRuntimeRevisionRepository,
    InMemoryPersonaDistillationItemRepository,
    InMemoryPersonaDistillationRunRepository,
    InMemoryPersonaRepository,
    InMemoryPersonaSourceRepository,
    InMemoryPersonaVersionRepository,
    InMemoryPublicEndpointRepository,
    InMemoryUserRepository,
    InMemoryWorkflowCatalogRepository,
    SQLMemoryRepository,
    InMemoryUploadedDocumentRepository,
    SQLGraphProjectionEventRepository,
    SQLPersonaDistillationItemRepository,
    SQLPersonaDistillationRunRepository,
    SQLPersonaRepository,
    SQLPersonaSourceRepository,
    SQLPersonaVersionRepository,
    SQLUploadedDocumentRepository,
    ensure_builtin_runtime_adapters,
)
from app.db.repositories.domain_sql import (
    SQLAgentRepository,
    SQLApiTokenRepository,
    SQLChannelIdentityMappingRepository,
    SQLConnectorInstallationRepository,
    SQLConversationMessageRepository,
    SQLConversationRepository,
    SQLConversationApprovalRequestRepository,
    SQLCredentialRepository,
    SQLGoalRepository,
    SQLMainAgentProfileRepository,
    SQLMCPServerRepository,
    SQLModelProfileRepository,
    SQLModelProviderRepository,
    SQLOneCLIIdentityMappingRepository,
    SQLPublicEndpointRepository,
    SQLRuntimeAdapterRepository,
    SQLRuntimeRevisionRepository,
    SQLScheduleRepository,
    SQLToolRepository,
    SQLUserRepository,
    SQLWorkflowRepository,
)
from app.db.repositories.schedules import InMemoryScheduleRepository
from app.db.session import get_session_maker, is_database_configured
from app.domain import (
    AgentDefinition,
    ChannelIdentityMapping,
    ConnectorInstallation,
    CredentialReference,
    CredentialDefinition,
    GoalDefinition,
    MainAgentProfile,
    MCPServerDefinition,
    MCPTransportType,
    ModelProviderDefinition,
    OneCLIIdentityMapping,
    RuntimeAdapterDefinition,
    ToolDefinition,
)
from app.llm.registry import LLMEnvironmentConfig, ModelProviderRegistry

# Register the Codex model provider during context bootstrap.
importlib.import_module("app.llm.openai_codex")
from app.protocols.mcp.registry import MCPClientRegistry
from app.runtime.adapters.crewai.adapter import CrewAIRuntimeAdapter
from app.runtime.adapters.native_adapter import NativeRuntimeAdapter
from app.runtime.containers import DockerRuntimeManager
from app.runtime.control_plane import ExecutionControlPlane
from app.runtime.governance.compaction import RuntimeContextCompactor
from app.runtime.native.approvals import ApprovalDecision, ApprovalManager
from app.runtime.native.engine import ExecutionEngine
from app.runtime.native.graph_context import RuntimeGraphContextAutoRetriever
from app.runtime.native.memory import SharedMemoryPromptBuilder
from app.runtime.native.state import InMemoryExecutionStore, SQLExecutionStore
from app.runtime.operations import RuntimeOperationsRecorder
from app.runtime.reconcile import RuntimeReconciler
from app.runtime.registry import RuntimeAdapterRegistry
from app.runtime.revisions import RuntimeRevisionService
from app.scheduler.scheduler import WorkflowScheduler
from app.tools.registry import ToolRegistry
from app.tools.service import ToolService
from app.tools.builtins import builtin_tool_definitions


@dataclass
class ApiContext:
    """Container for repositories and long-lived runtime collaborators.

    Keep this object infrastructure-focused. Domain behavior should live in
    services, repositories, or runtime adapters so tests can swap context
    dependencies without bypassing production code paths.
    """

    COMPUTER_USE_MACOS_MCP_SERVER_ID = "computer-use-macos"
    COMPUTER_USE_WINDOWS_MCP_SERVER_ID = "computer-use-windows"
    FIRECRAWL_MCP_SERVER_ID = "research-firecrawl"
    CONTEXT7_MCP_SERVER_ID = "docs-context7"

    agent_repo: object
    conversation_repo: object
    channel_identity_mapping_repo: object
    conversation_message_repo: object
    conversation_approval_repo: object
    memory_repo: object
    uploaded_document_repo: object
    persona_repo: object
    persona_version_repo: object
    persona_source_repo: object
    persona_distillation_run_repo: object
    persona_distillation_item_repo: object
    main_agent_profile_repo: object
    conversation_event_broker: ConversationEventBroker
    credential_repo: object
    connector_installation_repo: object
    public_endpoint_repo: object
    api_token_repo: object
    user_repo: object
    onecli_identity_mapping_repo: object
    tool_repo: object
    workflow_repo: object
    graph_projection_event_repo: object
    goal_repo: object
    model_provider_repo: object
    model_profile_repo: object
    schedule_repo: object
    runtime_adapter_repo: object
    runtime_revision_repo: object
    mcp_server_repo: object
    execution_store: object
    llm_provider_registry: ModelProviderRegistry
    execution_engine: ExecutionEngine
    runtime_registry: RuntimeAdapterRegistry
    control_plane: ExecutionControlPlane
    tool_service: ToolService
    mcp_registry: MCPClientRegistry
    scheduler: WorkflowScheduler
    runtime_revision_service: RuntimeRevisionService
    runtime_container_manager: DockerRuntimeManager
    runtime_reconciler: RuntimeReconciler
    runtime_operations: RuntimeOperationsRecorder
    database_health_checks_enabled: bool = True

    async def ensure_runtime_adapter_seed_data(self) -> None:
        """Ensure built-in runtime adapter records are present in the configured repository."""
        await ensure_builtin_runtime_adapters(self.runtime_adapter_repo)

    async def ensure_builtin_tool_seed_data(self) -> list[ToolDefinition]:
        """Persist built-in app tools and system tools without overwriting existing records."""
        # Startup seeding should persist the same builtin registry that CLI discovery and
        # runtime inspection expose, otherwise builtin families can drift between codepaths.
        tools = builtin_tool_definitions()
        saved: list[ToolDefinition] = []
        seen: set[str] = set()
        for tool in tools:
            if tool.id in seen:
                continue
            seen.add(tool.id)
            existing = await self.tool_repo.get(tool.id, include_deleted=True)
            if existing is None:
                saved.append(await self.tool_repo.create(tool))
            else:
                saved.append(existing)
        return saved

    def _host_platform(self) -> str:
        if sys.platform == "darwin":
            return "macos"
        if sys.platform.startswith("win"):
            return "windows"
        return "other"

    def _computer_use_server_specs(self) -> list[MCPServerDefinition]:
        macos_command = os.getenv("COMPUTER_USE_MACOS_MCP_COMMAND", "uvx")
        macos_args = shlex.split(os.getenv("COMPUTER_USE_MACOS_MCP_ARGS", "macos-mcp"))
        windows_command = os.getenv("COMPUTER_USE_WINDOWS_MCP_COMMAND", "uvx")
        windows_args = shlex.split(os.getenv("COMPUTER_USE_WINDOWS_MCP_ARGS", "windows-mcp"))
        return [
            MCPServerDefinition(
                id=self.COMPUTER_USE_MACOS_MCP_SERVER_ID,
                name="Computer Use macOS MCP",
                transport=MCPTransportType.STDIO,
                command=macos_command,
                args=macos_args,
                enabled=True,
                allowlisted_command=Path(macos_command).name,
                metadata={
                    "system": True,
                    "family": "computer_use",
                    "platform": "macos",
                    "distribution": "external",
                    "source_repository": "https://github.com/CursorTouch/MacOS-MCP",
                },
            ),
            MCPServerDefinition(
                id=self.COMPUTER_USE_WINDOWS_MCP_SERVER_ID,
                name="Computer Use Windows MCP",
                transport=MCPTransportType.STDIO,
                command=windows_command,
                args=windows_args,
                enabled=True,
                allowlisted_command=Path(windows_command).name,
                metadata={
                    "system": True,
                    "family": "computer_use",
                    "platform": "windows",
                    "distribution": "external",
                    "source_repository": "https://github.com/CursorTouch/Windows-MCP",
                },
            ),
        ]

    def _env_bool(self, name: str, *, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def _optional_env_value(self, name: str, fallback: str | None = None) -> str | None:
        raw = os.getenv(name)
        if raw is None:
            raw = fallback
        if raw is None:
            return None
        normalized = raw.strip()
        return normalized or None

    def _optional_env_bool(self, name: str, fallback: str | None, *, default: bool) -> bool:
        raw = self._optional_env_value(name, fallback)
        if raw is None:
            return default
        return raw.lower() in {"1", "true", "yes", "on"}

    def _firecrawl_server_spec(self) -> MCPServerDefinition:
        settings = get_settings()
        command = self._optional_env_value("FIRECRAWL_MCP_COMMAND", settings.firecrawl_mcp_command) or "npx"
        args = shlex.split(
            self._optional_env_value("FIRECRAWL_MCP_ARGS", settings.firecrawl_mcp_args) or "-y firecrawl-mcp"
        )
        api_key_ref = (
                self._optional_env_value("FIRECRAWL_MCP_API_KEY_REF", settings.firecrawl_mcp_api_key_ref)
                or "env://FIRECRAWL_API_KEY"
        )
        api_key = self._optional_env_value("FIRECRAWL_API_KEY", settings.firecrawl_api_key)
        enabled = self._optional_env_bool(
            "FIRECRAWL_MCP_ENABLED",
            settings.firecrawl_mcp_enabled,
            default=bool(api_key),
        )
        return MCPServerDefinition(
            id=self.FIRECRAWL_MCP_SERVER_ID,
            name="Firecrawl MCP",
            transport=MCPTransportType.STDIO,
            command=command,
            args=args,
            env_refs=[
                CredentialReference(
                    ref=api_key_ref,
                    source="env",
                    key="FIRECRAWL_API_KEY",
                    description="Firecrawl API key injected into the Firecrawl MCP server process.",
                )
            ],
            enabled=enabled,
            allowlisted_command=Path(command).name,
            metadata={
                "system": True,
                "family": "web_research",
                "provider": "firecrawl",
                "distribution": "external",
                "source_repository": "https://github.com/mendableai/firecrawl",
            },
        )

    def _context7_server_spec(self) -> MCPServerDefinition:
        settings = get_settings()
        command = self._optional_env_value("CONTEXT7_MCP_COMMAND", settings.context7_mcp_command) or "npx"
        args = shlex.split(
            self._optional_env_value("CONTEXT7_MCP_ARGS", settings.context7_mcp_args)
            or "-y @upstash/context7-mcp"
        )
        api_key_ref = (
                self._optional_env_value("CONTEXT7_MCP_API_KEY_REF", settings.context7_mcp_api_key_ref)
                or "env://CONTEXT7_API_KEY"
        )
        api_key = self._optional_env_value("CONTEXT7_API_KEY", settings.context7_api_key)
        enabled = self._optional_env_bool(
            "CONTEXT7_MCP_ENABLED",
            settings.context7_mcp_enabled,
            default=bool(api_key),
        )
        env_refs = []
        if api_key:
            env_refs.append(
                CredentialReference(
                    ref=api_key_ref,
                    source="env",
                    key="CONTEXT7_API_KEY",
                    description="Context7 API key injected into the Context7 MCP server process.",
                )
            )
        return MCPServerDefinition(
            id=self.CONTEXT7_MCP_SERVER_ID,
            name="Context7 MCP",
            transport=MCPTransportType.STDIO,
            command=command,
            args=args,
            env_refs=env_refs,
            enabled=enabled,
            allowlisted_command=Path(command).name,
            metadata={
                "system": True,
                "family": "developer_docs",
                "provider": "context7",
                "distribution": "external",
                "source_repository": "https://github.com/upstash/context7",
            },
        )

    def builtin_computer_use_server_ids_for_host(self) -> list[str]:
        """Return the built-in Computer Use MCP server IDs that match the host platform."""
        host_platform = self._host_platform()
        if host_platform == "macos":
            return [self.COMPUTER_USE_MACOS_MCP_SERVER_ID]
        if host_platform == "windows":
            return [self.COMPUTER_USE_WINDOWS_MCP_SERVER_ID]
        return []

    def builtin_mcp_server_ids_for_startup_discovery(self) -> list[str]:
        server_ids = list(self.builtin_computer_use_server_ids_for_host())
        if self._firecrawl_server_spec().enabled:
            server_ids.append(self.FIRECRAWL_MCP_SERVER_ID)
        if self._context7_server_spec().enabled:
            server_ids.append(self.CONTEXT7_MCP_SERVER_ID)
        return server_ids

    async def ensure_builtin_computer_use_mcp_server_seed_data(self) -> dict[str, MCPServerDefinition]:
        """Create or refresh built-in Computer Use MCP server records for discovery."""
        saved: dict[str, MCPServerDefinition] = {}
        for server in self._computer_use_server_specs():
            existing = await self.mcp_server_repo.get(server.id, include_deleted=True)
            if existing is not None:
                merged = existing.model_dump(mode="json")
                merged.update(server.model_dump(mode="json"))
                server = MCPServerDefinition.model_validate(merged)
            saved[server.id] = await self.mcp_server_repo.save(server)
        return saved

    async def ensure_builtin_research_mcp_server_seed_data(self) -> dict[str, MCPServerDefinition]:
        """Create or refresh built-in research MCP server records."""
        saved: dict[str, MCPServerDefinition] = {}
        for server in [self._firecrawl_server_spec(), self._context7_server_spec()]:
            existing = await self.mcp_server_repo.get(server.id, include_deleted=True)
            if existing is not None:
                merged = existing.model_dump(mode="json")
                merged.update(server.model_dump(mode="json"))
                server = MCPServerDefinition.model_validate(merged)
            saved[server.id] = await self.mcp_server_repo.save(server)
        return saved

    async def sync_mcp_catalog(self, server_id: str | None = None) -> dict:
        if server_id:
            definition = await self.mcp_server_repo.get(server_id)
            if definition is not None:
                self.mcp_registry.register(definition)
        else:
            for definition in await self.mcp_server_repo.list():
                self.mcp_registry.register(definition)
        tool_definitions = self.mcp_registry.discovered_tool_definitions(server_id)
        for tool in tool_definitions:
            existing = await self.tool_repo.get(tool.id)
            if existing is None:
                await self.tool_repo.create(tool)
            else:
                await self.tool_repo.update(tool.id, tool.model_dump(mode="json"))
        return {
            "tools": [tool.model_dump(mode="json") for tool in tool_definitions],
            "resources": self.mcp_registry.discovered_resources(server_id),
            "prompts": self.mcp_registry.discovered_prompts(server_id),
        }

    async def ensure_builtin_mcp_servers_seed_data(self) -> dict[str, MCPServerDefinition]:
        return {
            **await self.ensure_builtin_computer_use_mcp_server_seed_data(),
            **await self.ensure_builtin_research_mcp_server_seed_data(),
        }


_MAIN_AGENT_DELEGATION_DENY_RISK_LABELS = {
    "browser",
    "credentials",
    "dangerous",
    "filesystem",
    "local_privileged_execution",
    "mcp",
    "mutation",
    "network",
    "shell",
}


def _main_agent_hitl_delegate_decision_provider(*, execution_store: object, workflow_repo: object):
    async def decide(
            *,
            execution_id: str,
            tool_id: str,
            payload: dict[str, Any],
            approval_metadata: dict[str, Any],
    ) -> ApprovalDecision | None:
        execution = await execution_store.get_execution(execution_id)
        if execution is None:
            return None
        workflow = await workflow_repo.get(execution.workflow_id)
        if workflow is None:
            return None
        monitoring = workflow.metadata.get("main_agent_monitoring") if isinstance(workflow.metadata, dict) else None
        if not isinstance(monitoring, dict) or monitoring.get("delegate_hitl_to_main_agent") is not True:
            return None

        risk_labels = {str(label) for label in approval_metadata.get("risk_labels") or []}
        denied_labels = sorted(risk_labels.intersection(_MAIN_AGENT_DELEGATION_DENY_RISK_LABELS))
        if denied_labels or approval_metadata.get("local_privileged_execution") is True:
            return None

        return ApprovalDecision(
            granted=True,
            reason="Main-agent delegated HITL approval: low-risk approval request allowed by workflow policy.",
            metadata={
                "mode": "delegated",
                "delegate": "main_agent",
                "policy": "main_agent_monitoring.delegate_hitl_to_main_agent",
                "workflow_id": execution.workflow_id,
                "tool_id": tool_id,
                "tool_name": approval_metadata.get("tool_name"),
                "agent_id": approval_metadata.get("agent_id"),
                "task_id": approval_metadata.get("task_id"),
                "risk_labels": sorted(risk_labels),
                "risk_gate": "low_risk_only",
            },
        )

    return decide


def _attach_goal_supervisor_waker(context: ApiContext) -> None:
    async def wake_goal_supervisor(goal_id: str) -> dict[str, Any]:
        # Import lazily so ApiContext remains the composition root without a module-level service cycle.
        from app.services.main_agent_workflow_monitor import MainAgentWorkflowMonitorService

        result = await MainAgentWorkflowMonitorService(context).run_for_goal(goal_id)
        context.runtime_operations.record_action(
            "main_agent_monitor.goal_wake",
            goal_id=goal_id,
            finding_count=result.get("finding_count", 0),
            goal_finding_count=result.get("goal_finding_count", 0),
        )
        return result

    context.scheduler.goal_supervisor_waker = wake_goal_supervisor


@lru_cache(maxsize=1)
def get_default_api_context() -> ApiContext:
    if not is_database_configured():
        raise RuntimeError("DATABASE_URL is required for the default API context in Phase C8")
    session_factory = get_session_maker()
    if session_factory is None:
        raise RuntimeError("Database session factory is unavailable")
    agent_repo = SQLAgentRepository(session_factory)
    conversation_repo = SQLConversationRepository(session_factory)
    channel_identity_mapping_repo = SQLChannelIdentityMappingRepository(session_factory)
    conversation_message_repo = SQLConversationMessageRepository(session_factory)
    conversation_approval_repo = SQLConversationApprovalRequestRepository(session_factory)
    memory_repo = SQLMemoryRepository(session_factory)
    uploaded_document_repo = SQLUploadedDocumentRepository(session_factory)
    persona_repo = SQLPersonaRepository(session_factory)
    persona_version_repo = SQLPersonaVersionRepository(session_factory)
    persona_source_repo = SQLPersonaSourceRepository(session_factory)
    persona_distillation_run_repo = SQLPersonaDistillationRunRepository(session_factory)
    persona_distillation_item_repo = SQLPersonaDistillationItemRepository(session_factory)
    main_agent_profile_repo = SQLMainAgentProfileRepository(session_factory)
    conversation_event_broker = ConversationEventBroker()
    credential_repo = SQLCredentialRepository(session_factory)
    connector_installation_repo = SQLConnectorInstallationRepository(session_factory)
    public_endpoint_repo = SQLPublicEndpointRepository(session_factory)
    api_token_repo = SQLApiTokenRepository(session_factory)
    user_repo = SQLUserRepository(session_factory)
    onecli_identity_mapping_repo = SQLOneCLIIdentityMappingRepository(session_factory)
    tool_repo = SQLToolRepository(session_factory)
    graph_projection_event_repo = SQLGraphProjectionEventRepository(session_factory)
    goal_repo = SQLGoalRepository(session_factory)
    workflow_repo = SQLWorkflowRepository(session_factory, graph_projection_event_repo=graph_projection_event_repo)
    model_provider_repo = SQLModelProviderRepository(session_factory)
    model_profile_repo = SQLModelProfileRepository(session_factory)
    schedule_repo = SQLScheduleRepository(session_factory)
    runtime_adapter_repo = SQLRuntimeAdapterRepository(session_factory)
    runtime_revision_repo = SQLRuntimeRevisionRepository(session_factory)
    mcp_server_repo = SQLMCPServerRepository(session_factory)
    execution_store = SQLExecutionStore(session_factory, graph_projection_event_repo=graph_projection_event_repo)
    env_config = LLMEnvironmentConfig.from_env(model_provider_repo=model_provider_repo)
    llm_provider_registry = ModelProviderRegistry.create_default(env_config=env_config)
    mcp_registry = MCPClientRegistry()
    approval_manager = ApprovalManager(
        execution_store=execution_store,
        delegate_decision_provider=_main_agent_hitl_delegate_decision_provider(
            execution_store=execution_store,
            workflow_repo=workflow_repo,
        ),
    )
    execution_engine = ExecutionEngine(
        workflow_repository=workflow_repo,
        model_profile_repository=model_profile_repo,
        execution_store=execution_store,
        model_provider_registry=llm_provider_registry,
        approval_manager=approval_manager,
    )
    runtime_registry = RuntimeAdapterRegistry(
        workflow_repository=workflow_repo,
        model_profile_repository=model_profile_repo,
        execution_store=execution_store,
    )
    runtime_registry.register(NativeRuntimeAdapter(execution_engine))
    crewai_adapter = CrewAIRuntimeAdapter(
        workflow_repository=workflow_repo,
        model_profile_repository=model_profile_repo,
        execution_store=execution_store,
        model_provider_registry=llm_provider_registry,
    )
    runtime_registry.register(crewai_adapter)
    tool_registry = ToolRegistry(
        approval_manager=approval_manager,
        runtime_registry=runtime_registry,
        mcp_registry=mcp_registry,
        execution_store=execution_store,
        tool_repository=tool_repo,
    )
    execution_engine.agent_executor.tool_executor.tool_registry.runtime_registry = runtime_registry
    execution_engine.agent_executor.tool_executor.tool_registry.mcp_registry = mcp_registry
    execution_engine.agent_executor.tool_executor.tool_registry.execution_store = execution_store
    runtime_revision_service = RuntimeRevisionService(runtime_revision_repo=runtime_revision_repo)
    runtime_container_manager = DockerRuntimeManager()
    runtime_operations = RuntimeOperationsRecorder()
    runtime_reconciler = RuntimeReconciler(
        execution_store=execution_store,
        runtime_container_manager=runtime_container_manager,
        operations=runtime_operations,
    )
    control_plane = ExecutionControlPlane(
        runtime_registry=runtime_registry,
        execution_store=execution_store,
        approval_manager=approval_manager,
        runtime_revision_service=runtime_revision_service,
        runtime_container_manager=runtime_container_manager,
        runtime_reconciler=runtime_reconciler,
        runtime_operations=runtime_operations,
    )
    tool_service = ToolService(
        tool_registry=tool_registry,
        execution_store=execution_store,
        approval_manager=approval_manager,
    )
    scheduler = WorkflowScheduler(
        schedule_repo=schedule_repo,
        execution_store=execution_store,
        runtime_registry=runtime_registry,
        execution_starter=control_plane.queue_start,
        goal_repo=goal_repo,
        runtime_operations=runtime_operations,
    )
    context = ApiContext(
        agent_repo=agent_repo,
        conversation_repo=conversation_repo,
        channel_identity_mapping_repo=channel_identity_mapping_repo,
        conversation_message_repo=conversation_message_repo,
        conversation_approval_repo=conversation_approval_repo,
        memory_repo=memory_repo,
        uploaded_document_repo=uploaded_document_repo,
        persona_repo=persona_repo,
        persona_version_repo=persona_version_repo,
        persona_source_repo=persona_source_repo,
        persona_distillation_run_repo=persona_distillation_run_repo,
        persona_distillation_item_repo=persona_distillation_item_repo,
        main_agent_profile_repo=main_agent_profile_repo,
        conversation_event_broker=conversation_event_broker,
        credential_repo=credential_repo,
        connector_installation_repo=connector_installation_repo,
        public_endpoint_repo=public_endpoint_repo,
        api_token_repo=api_token_repo,
        user_repo=user_repo,
        onecli_identity_mapping_repo=onecli_identity_mapping_repo,
        tool_repo=tool_repo,
        workflow_repo=workflow_repo,
        graph_projection_event_repo=graph_projection_event_repo,
        goal_repo=goal_repo,
        model_provider_repo=model_provider_repo,
        model_profile_repo=model_profile_repo,
        schedule_repo=schedule_repo,
        runtime_adapter_repo=runtime_adapter_repo,
        runtime_revision_repo=runtime_revision_repo,
        mcp_server_repo=mcp_server_repo,
        execution_store=execution_store,
        llm_provider_registry=llm_provider_registry,
        execution_engine=execution_engine,
        runtime_registry=runtime_registry,
        control_plane=control_plane,
        tool_service=tool_service,
        mcp_registry=mcp_registry,
        scheduler=scheduler,
        runtime_revision_service=runtime_revision_service,
        runtime_container_manager=runtime_container_manager,
        runtime_reconciler=runtime_reconciler,
        runtime_operations=runtime_operations,
    )

    async def _persist_run_summary(execution, workflow):
        from app.services.execution_run_summary import ExecutionRunSummaryService
        await ExecutionRunSummaryService(context).maybe_persist_run_summary(execution=execution, workflow=workflow)

    runtime_graph_context = RuntimeGraphContextAutoRetriever(context)
    execution_engine.set_memory_prompt_builder(SharedMemoryPromptBuilder(context))
    execution_engine.set_context_compactor(RuntimeContextCompactor(context))
    execution_engine.set_graph_context_retriever(runtime_graph_context.retrieve_before_subagent_start)
    execution_engine.set_execution_failure_graph_context_retriever(
        runtime_graph_context.retrieve_after_execution_failed
    )
    execution_engine.set_context_compaction_graph_context_retriever(
        runtime_graph_context.retrieve_after_context_compaction
    )
    execution_engine.set_proposal_tool_graph_context_retriever(
        runtime_graph_context.retrieve_before_proposal_tool
    )
    execution_engine.execution_completion_handler = _persist_run_summary
    crewai_adapter.execution_completion_handler = _persist_run_summary
    _attach_goal_supervisor_waker(context)
    return context


def create_worker_api_context(*, worker_id: str | None = None) -> ApiContext:
    context = get_default_api_context()
    if worker_id is not None:
        context.control_plane.worker_id = worker_id
    return context


def create_test_api_context() -> ApiContext:
    agent_repo = InMemoryCatalogRepository(AgentDefinition)
    conversation_repo = InMemoryConversationRepository()
    channel_identity_mapping_repo = InMemoryCatalogRepository(ChannelIdentityMapping)
    conversation_message_repo = InMemoryConversationMessageRepository()
    conversation_approval_repo = InMemoryConversationApprovalRequestRepository()
    memory_repo = InMemoryMemoryRepository()
    uploaded_document_repo = InMemoryUploadedDocumentRepository()
    persona_repo = InMemoryPersonaRepository()
    persona_version_repo = InMemoryPersonaVersionRepository()
    persona_source_repo = InMemoryPersonaSourceRepository()
    persona_distillation_run_repo = InMemoryPersonaDistillationRunRepository()
    persona_distillation_item_repo = InMemoryPersonaDistillationItemRepository()
    main_agent_profile_repo = InMemoryCatalogRepository(MainAgentProfile)
    conversation_event_broker = ConversationEventBroker()
    credential_repo = InMemoryCatalogRepository(CredentialDefinition)
    connector_installation_repo = InMemoryCatalogRepository(ConnectorInstallation)
    public_endpoint_repo = InMemoryPublicEndpointRepository()
    api_token_repo = InMemoryApiTokenRepository()
    user_repo = InMemoryUserRepository()
    onecli_identity_mapping_repo = InMemoryCatalogRepository(OneCLIIdentityMapping)
    tool_repo = InMemoryCatalogRepository(ToolDefinition)
    graph_projection_event_repo = InMemoryGraphProjectionEventRepository()
    goal_repo = InMemoryCatalogRepository(GoalDefinition)
    workflow_repo = InMemoryWorkflowCatalogRepository()
    model_provider_repo = InMemoryCatalogRepository(ModelProviderDefinition)
    model_profile_repo = InMemoryModelProfileCatalogRepository()
    schedule_repo = InMemoryScheduleRepository()
    runtime_adapter_repo = InMemoryCatalogRepository(RuntimeAdapterDefinition)
    runtime_revision_repo = InMemoryRuntimeRevisionRepository()
    mcp_server_repo = InMemoryCatalogRepository(MCPServerDefinition)
    execution_store = InMemoryExecutionStore()
    llm_provider_registry = ModelProviderRegistry()
    mcp_registry = MCPClientRegistry()
    approval_manager = ApprovalManager(
        execution_store=execution_store,
        delegate_decision_provider=_main_agent_hitl_delegate_decision_provider(
            execution_store=execution_store,
            workflow_repo=workflow_repo,
        ),
    )
    execution_engine = ExecutionEngine(
        workflow_repository=workflow_repo,
        model_profile_repository=model_profile_repo,
        execution_store=execution_store,
        model_provider_registry=llm_provider_registry,
        approval_manager=approval_manager,
    )
    runtime_registry = RuntimeAdapterRegistry(
        workflow_repository=workflow_repo,
        model_profile_repository=model_profile_repo,
        execution_store=execution_store,
    )
    runtime_registry.register(NativeRuntimeAdapter(execution_engine))
    crewai_adapter = CrewAIRuntimeAdapter(
        workflow_repository=workflow_repo,
        model_profile_repository=model_profile_repo,
        execution_store=execution_store,
    )
    runtime_registry.register(crewai_adapter)
    tool_registry = ToolRegistry(
        approval_manager=approval_manager,
        runtime_registry=runtime_registry,
        mcp_registry=mcp_registry,
        execution_store=execution_store,
        tool_repository=tool_repo,
    )
    execution_engine.agent_executor.tool_executor.tool_registry.runtime_registry = runtime_registry
    execution_engine.agent_executor.tool_executor.tool_registry.mcp_registry = mcp_registry
    execution_engine.agent_executor.tool_executor.tool_registry.execution_store = execution_store
    runtime_revision_service = RuntimeRevisionService(runtime_revision_repo=runtime_revision_repo)
    runtime_container_manager = DockerRuntimeManager()
    runtime_operations = RuntimeOperationsRecorder()
    runtime_reconciler = RuntimeReconciler(
        execution_store=execution_store,
        runtime_container_manager=runtime_container_manager,
        operations=runtime_operations,
    )
    control_plane = ExecutionControlPlane(
        runtime_registry=runtime_registry,
        execution_store=execution_store,
        approval_manager=approval_manager,
        runtime_revision_service=runtime_revision_service,
        runtime_container_manager=runtime_container_manager,
        runtime_reconciler=runtime_reconciler,
        runtime_operations=runtime_operations,
        execution_isolation_enabled=False,
        worker_id="test-worker",
        stale_after_seconds=1,
    )
    tool_service = ToolService(
        tool_registry=tool_registry,
        execution_store=execution_store,
        approval_manager=approval_manager,
    )
    scheduler = WorkflowScheduler(
        schedule_repo=schedule_repo,
        execution_store=execution_store,
        runtime_registry=runtime_registry,
        execution_starter=control_plane.queue_start,
        goal_repo=goal_repo,
        runtime_operations=runtime_operations,
    )
    context = ApiContext(
        agent_repo=agent_repo,
        conversation_repo=conversation_repo,
        channel_identity_mapping_repo=channel_identity_mapping_repo,
        conversation_message_repo=conversation_message_repo,
        conversation_approval_repo=conversation_approval_repo,
        memory_repo=memory_repo,
        uploaded_document_repo=uploaded_document_repo,
        persona_repo=persona_repo,
        persona_version_repo=persona_version_repo,
        persona_source_repo=persona_source_repo,
        persona_distillation_run_repo=persona_distillation_run_repo,
        persona_distillation_item_repo=persona_distillation_item_repo,
        main_agent_profile_repo=main_agent_profile_repo,
        conversation_event_broker=conversation_event_broker,
        credential_repo=credential_repo,
        connector_installation_repo=connector_installation_repo,
        public_endpoint_repo=public_endpoint_repo,
        api_token_repo=api_token_repo,
        user_repo=user_repo,
        onecli_identity_mapping_repo=onecli_identity_mapping_repo,
        tool_repo=tool_repo,
        workflow_repo=workflow_repo,
        graph_projection_event_repo=graph_projection_event_repo,
        goal_repo=goal_repo,
        model_provider_repo=model_provider_repo,
        model_profile_repo=model_profile_repo,
        schedule_repo=schedule_repo,
        runtime_adapter_repo=runtime_adapter_repo,
        runtime_revision_repo=runtime_revision_repo,
        mcp_server_repo=mcp_server_repo,
        execution_store=execution_store,
        llm_provider_registry=llm_provider_registry,
        execution_engine=execution_engine,
        runtime_registry=runtime_registry,
        control_plane=control_plane,
        tool_service=tool_service,
        mcp_registry=mcp_registry,
        scheduler=scheduler,
        runtime_revision_service=runtime_revision_service,
        runtime_container_manager=runtime_container_manager,
        runtime_reconciler=runtime_reconciler,
        runtime_operations=runtime_operations,
        database_health_checks_enabled=False,
    )

    async def _persist_run_summary(execution, workflow):
        from app.services.execution_run_summary import ExecutionRunSummaryService
        await ExecutionRunSummaryService(context).maybe_persist_run_summary(execution=execution, workflow=workflow)

    runtime_graph_context = RuntimeGraphContextAutoRetriever(context)
    execution_engine.set_memory_prompt_builder(SharedMemoryPromptBuilder(context))
    execution_engine.set_context_compactor(RuntimeContextCompactor(context))
    execution_engine.set_graph_context_retriever(runtime_graph_context.retrieve_before_subagent_start)
    execution_engine.set_execution_failure_graph_context_retriever(
        runtime_graph_context.retrieve_after_execution_failed
    )
    execution_engine.set_context_compaction_graph_context_retriever(
        runtime_graph_context.retrieve_after_context_compaction
    )
    execution_engine.set_proposal_tool_graph_context_retriever(
        runtime_graph_context.retrieve_before_proposal_tool
    )
    execution_engine.execution_completion_handler = _persist_run_summary
    crewai_adapter.execution_completion_handler = _persist_run_summary
    _attach_goal_supervisor_waker(context)
    return context


def create_database_test_api_context() -> ApiContext:
    session_factory = get_session_maker()
    if session_factory is None:
        raise RuntimeError("DATABASE_URL is required for database-backed test context")
    agent_repo = SQLAgentRepository(session_factory)
    conversation_repo = SQLConversationRepository(session_factory)
    channel_identity_mapping_repo = SQLChannelIdentityMappingRepository(session_factory)
    conversation_message_repo = SQLConversationMessageRepository(session_factory)
    conversation_approval_repo = SQLConversationApprovalRequestRepository(session_factory)
    memory_repo = SQLMemoryRepository(session_factory)
    uploaded_document_repo = SQLUploadedDocumentRepository(session_factory)
    persona_repo = SQLPersonaRepository(session_factory)
    persona_version_repo = SQLPersonaVersionRepository(session_factory)
    persona_source_repo = SQLPersonaSourceRepository(session_factory)
    persona_distillation_run_repo = SQLPersonaDistillationRunRepository(session_factory)
    persona_distillation_item_repo = SQLPersonaDistillationItemRepository(session_factory)
    main_agent_profile_repo = SQLMainAgentProfileRepository(session_factory)
    conversation_event_broker = ConversationEventBroker()
    credential_repo = SQLCredentialRepository(session_factory)
    connector_installation_repo = SQLConnectorInstallationRepository(session_factory)
    public_endpoint_repo = SQLPublicEndpointRepository(session_factory)
    api_token_repo = SQLApiTokenRepository(session_factory)
    user_repo = SQLUserRepository(session_factory)
    onecli_identity_mapping_repo = SQLOneCLIIdentityMappingRepository(session_factory)
    tool_repo = SQLToolRepository(session_factory)
    graph_projection_event_repo = SQLGraphProjectionEventRepository(session_factory)
    goal_repo = SQLGoalRepository(session_factory)
    workflow_repo = SQLWorkflowRepository(session_factory, graph_projection_event_repo=graph_projection_event_repo)
    model_provider_repo = SQLModelProviderRepository(session_factory)
    model_profile_repo = SQLModelProfileRepository(session_factory)
    schedule_repo = SQLScheduleRepository(session_factory)
    runtime_adapter_repo = SQLRuntimeAdapterRepository(session_factory)
    runtime_revision_repo = SQLRuntimeRevisionRepository(session_factory)
    mcp_server_repo = SQLMCPServerRepository(session_factory)
    execution_store = SQLExecutionStore(session_factory, graph_projection_event_repo=graph_projection_event_repo)
    llm_provider_registry = ModelProviderRegistry()
    mcp_registry = MCPClientRegistry()
    approval_manager = ApprovalManager(
        execution_store=execution_store,
        delegate_decision_provider=_main_agent_hitl_delegate_decision_provider(
            execution_store=execution_store,
            workflow_repo=workflow_repo,
        ),
    )
    execution_engine = ExecutionEngine(
        workflow_repository=workflow_repo,
        model_profile_repository=model_profile_repo,
        execution_store=execution_store,
        model_provider_registry=llm_provider_registry,
        approval_manager=approval_manager,
    )
    runtime_registry = RuntimeAdapterRegistry(
        workflow_repository=workflow_repo,
        model_profile_repository=model_profile_repo,
        execution_store=execution_store,
    )
    runtime_registry.register(NativeRuntimeAdapter(execution_engine))
    crewai_adapter = CrewAIRuntimeAdapter(
        workflow_repository=workflow_repo,
        model_profile_repository=model_profile_repo,
        execution_store=execution_store,
    )
    runtime_registry.register(crewai_adapter)
    tool_registry = ToolRegistry(
        approval_manager=approval_manager,
        runtime_registry=runtime_registry,
        mcp_registry=mcp_registry,
        execution_store=execution_store,
        tool_repository=tool_repo,
    )
    execution_engine.agent_executor.tool_executor.tool_registry.runtime_registry = runtime_registry
    execution_engine.agent_executor.tool_executor.tool_registry.mcp_registry = mcp_registry
    execution_engine.agent_executor.tool_executor.tool_registry.execution_store = execution_store
    runtime_revision_service = RuntimeRevisionService(runtime_revision_repo=runtime_revision_repo)
    runtime_container_manager = DockerRuntimeManager()
    runtime_operations = RuntimeOperationsRecorder()
    runtime_reconciler = RuntimeReconciler(
        execution_store=execution_store,
        runtime_container_manager=runtime_container_manager,
        operations=runtime_operations,
    )
    control_plane = ExecutionControlPlane(
        runtime_registry=runtime_registry,
        execution_store=execution_store,
        approval_manager=approval_manager,
        runtime_revision_service=runtime_revision_service,
        runtime_container_manager=runtime_container_manager,
        runtime_reconciler=runtime_reconciler,
        runtime_operations=runtime_operations,
        worker_id="db-test-worker",
        stale_after_seconds=1,
    )
    tool_service = ToolService(
        tool_registry=tool_registry,
        execution_store=execution_store,
        approval_manager=approval_manager,
    )
    scheduler = WorkflowScheduler(
        schedule_repo=schedule_repo,
        execution_store=execution_store,
        runtime_registry=runtime_registry,
        execution_starter=control_plane.queue_start,
        goal_repo=goal_repo,
        runtime_operations=runtime_operations,
    )
    context = ApiContext(
        agent_repo=agent_repo,
        conversation_repo=conversation_repo,
        channel_identity_mapping_repo=channel_identity_mapping_repo,
        conversation_message_repo=conversation_message_repo,
        conversation_approval_repo=conversation_approval_repo,
        memory_repo=memory_repo,
        uploaded_document_repo=uploaded_document_repo,
        persona_repo=persona_repo,
        persona_version_repo=persona_version_repo,
        persona_source_repo=persona_source_repo,
        persona_distillation_run_repo=persona_distillation_run_repo,
        persona_distillation_item_repo=persona_distillation_item_repo,
        main_agent_profile_repo=main_agent_profile_repo,
        conversation_event_broker=conversation_event_broker,
        credential_repo=credential_repo,
        connector_installation_repo=connector_installation_repo,
        public_endpoint_repo=public_endpoint_repo,
        api_token_repo=api_token_repo,
        user_repo=user_repo,
        onecli_identity_mapping_repo=onecli_identity_mapping_repo,
        tool_repo=tool_repo,
        workflow_repo=workflow_repo,
        graph_projection_event_repo=graph_projection_event_repo,
        goal_repo=goal_repo,
        model_provider_repo=model_provider_repo,
        model_profile_repo=model_profile_repo,
        schedule_repo=schedule_repo,
        runtime_adapter_repo=runtime_adapter_repo,
        runtime_revision_repo=runtime_revision_repo,
        mcp_server_repo=mcp_server_repo,
        execution_store=execution_store,
        llm_provider_registry=llm_provider_registry,
        execution_engine=execution_engine,
        runtime_registry=runtime_registry,
        control_plane=control_plane,
        tool_service=tool_service,
        mcp_registry=mcp_registry,
        scheduler=scheduler,
        runtime_revision_service=runtime_revision_service,
        runtime_container_manager=runtime_container_manager,
        runtime_reconciler=runtime_reconciler,
        runtime_operations=runtime_operations,
    )

    async def _persist_run_summary(execution, workflow):
        from app.services.execution_run_summary import ExecutionRunSummaryService
        await ExecutionRunSummaryService(context).maybe_persist_run_summary(execution=execution, workflow=workflow)

    runtime_graph_context = RuntimeGraphContextAutoRetriever(context)
    execution_engine.set_memory_prompt_builder(SharedMemoryPromptBuilder(context))
    execution_engine.set_context_compactor(RuntimeContextCompactor(context))
    execution_engine.set_graph_context_retriever(runtime_graph_context.retrieve_before_subagent_start)
    execution_engine.set_execution_failure_graph_context_retriever(
        runtime_graph_context.retrieve_after_execution_failed
    )
    execution_engine.set_context_compaction_graph_context_retriever(
        runtime_graph_context.retrieve_after_context_compaction
    )
    execution_engine.set_proposal_tool_graph_context_retriever(
        runtime_graph_context.retrieve_before_proposal_tool
    )
    execution_engine.execution_completion_handler = _persist_run_summary
    crewai_adapter.execution_completion_handler = _persist_run_summary
    _attach_goal_supervisor_waker(context)
    return context
