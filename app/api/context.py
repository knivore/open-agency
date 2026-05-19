from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.conversation_streaming import ConversationEventBroker
from app.db.repositories import (
    InMemoryCatalogRepository,
    InMemoryConversationApprovalRequestRepository,
    InMemoryConversationMessageRepository,
    InMemoryConversationRepository,
    InMemoryMemoryRepository,
    InMemoryModelProfileCatalogRepository,
    InMemoryUserRepository,
    InMemoryWorkflowCatalogRepository,
    SQLMemoryRepository,
    ensure_builtin_runtime_adapters,
)
from app.db.repositories.domain_sql import (
    SQLAgentRepository,
    SQLChannelIdentityMappingRepository,
    SQLConversationMessageRepository,
    SQLConversationRepository,
    SQLConversationApprovalRequestRepository,
    SQLCredentialRepository,
    SQLMainAgentProfileRepository,
    SQLMCPServerRepository,
    SQLModelProfileRepository,
    SQLModelProviderRepository,
    SQLRuntimeAdapterRepository,
    SQLRuntimeRevisionRepository,
    SQLScheduleRepository,
    SQLToolRepository,
    SQLWorkflowRepository,
)
from app.db.repositories.schedules import InMemoryScheduleRepository
from app.db.session import get_session_maker, is_database_configured
from app.domain import (
    AgentDefinition,
    ChannelIdentityMapping,
    CredentialDefinition,
    MainAgentProfile,
    MCPServerDefinition,
    MCPTransportType,
    ModelProviderDefinition,
    RuntimeAdapterDefinition,
    RuntimeRevision,
    ToolDefinition,
)
from app.llm.registry import LLMEnvironmentConfig, ModelProviderRegistry
from app.llm.openai_codex import OpenAICodexModelClient # Force import to ensure registration
from app.protocols.mcp import MCPClientRegistry
from app.runtime.adapters import CrewAIRuntimeAdapter, NativeRuntimeAdapter
from app.runtime.containers import DockerRuntimeManager
from app.runtime.control_plane import ExecutionControlPlane
from app.runtime.native.approvals import ApprovalManager
from app.runtime.native.engine import ExecutionEngine
from app.runtime.native.memory import SharedMemoryPromptBuilder
from app.runtime.native.state import InMemoryExecutionStore, SQLExecutionStore
from app.runtime.operations import RuntimeOperationsRecorder
from app.runtime.reconcile import RuntimeReconciler
from app.runtime.registry import RuntimeAdapterRegistry
from app.runtime.revisions import RuntimeRevisionService
from app.scheduler import WorkflowScheduler
from app.tools import ToolRegistry, ToolService
from app.tools.definitions import get_tool_catalog_definitions
from app.services.agent_tools import (
    command_system_tool_definitions,
    execution_system_tool_definitions,
    memory_system_tool_definitions,
    tool_management_system_tool_definitions,
    workflow_system_tool_definitions,
)


@dataclass
class ApiContext:
    COMPUTER_USE_MACOS_MCP_SERVER_ID = "computer-use-macos"
    COMPUTER_USE_WINDOWS_MCP_SERVER_ID = "computer-use-windows"

    agent_repo: object
    conversation_repo: object
    channel_identity_mapping_repo: object
    conversation_message_repo: object
    conversation_approval_repo: object
    memory_repo: object
    main_agent_profile_repo: object
    conversation_event_broker: ConversationEventBroker
    credential_repo: object
    user_repo: object
    tool_repo: object
    workflow_repo: object
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

    async def ensure_runtime_adapter_seed_data(self) -> None:
        await ensure_builtin_runtime_adapters(self.runtime_adapter_repo)

    async def ensure_builtin_tool_seed_data(self) -> list[ToolDefinition]:
        tools = [
            *get_tool_catalog_definitions(),
            *workflow_system_tool_definitions(can_trigger_workflows=True),
            *tool_management_system_tool_definitions(can_manage_tools=True),
            *memory_system_tool_definitions(can_manage_memory=True),
            *execution_system_tool_definitions(can_inspect_executions=True),
            *command_system_tool_definitions(can_run_commands=True),
        ]
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

    def builtin_computer_use_server_ids_for_host(self) -> list[str]:
        host_platform = self._host_platform()
        if host_platform == "macos":
            return [self.COMPUTER_USE_MACOS_MCP_SERVER_ID]
        if host_platform == "windows":
            return [self.COMPUTER_USE_WINDOWS_MCP_SERVER_ID]
        return []

    async def ensure_builtin_computer_use_mcp_server_seed_data(self) -> dict[str, MCPServerDefinition]:
        saved: dict[str, MCPServerDefinition] = {}
        for server in self._computer_use_server_specs():
            allowlisted_command = Path(server.command).name
            self.mcp_registry.allowlisted_commands.add(allowlisted_command)
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
        return await self.ensure_builtin_computer_use_mcp_server_seed_data()


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
    main_agent_profile_repo = SQLMainAgentProfileRepository(session_factory)
    conversation_event_broker = ConversationEventBroker()
    credential_repo = SQLCredentialRepository(session_factory)
    user_repo = InMemoryUserRepository()
    tool_repo = SQLToolRepository(session_factory)
    workflow_repo = SQLWorkflowRepository(session_factory)
    model_provider_repo = SQLModelProviderRepository(session_factory)
    model_profile_repo = SQLModelProfileRepository(session_factory)
    schedule_repo = SQLScheduleRepository(session_factory)
    runtime_adapter_repo = SQLRuntimeAdapterRepository(session_factory)
    runtime_revision_repo = SQLRuntimeRevisionRepository(session_factory)
    mcp_server_repo = SQLMCPServerRepository(session_factory)
    execution_store = SQLExecutionStore(session_factory)
    env_config = LLMEnvironmentConfig.from_env(model_provider_repo=model_provider_repo)
    llm_provider_registry = ModelProviderRegistry.create_default(env_config=env_config)
    mcp_registry = MCPClientRegistry()
    approval_manager = ApprovalManager(execution_store=execution_store)
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
        runtime_operations=runtime_operations,
    )
    context = ApiContext(
        agent_repo=agent_repo,
        conversation_repo=conversation_repo,
        channel_identity_mapping_repo=channel_identity_mapping_repo,
        conversation_message_repo=conversation_message_repo,
        conversation_approval_repo=conversation_approval_repo,
        memory_repo=memory_repo,
        main_agent_profile_repo=main_agent_profile_repo,
        conversation_event_broker=conversation_event_broker,
        credential_repo=credential_repo,
        user_repo=user_repo,
        tool_repo=tool_repo,
        workflow_repo=workflow_repo,
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

    execution_engine.set_memory_prompt_builder(SharedMemoryPromptBuilder(context))
    execution_engine.execution_completion_handler = _persist_run_summary
    crewai_adapter.execution_completion_handler = _persist_run_summary
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
    main_agent_profile_repo = InMemoryCatalogRepository(MainAgentProfile)
    conversation_event_broker = ConversationEventBroker()
    credential_repo = InMemoryCatalogRepository(CredentialDefinition)
    user_repo = InMemoryUserRepository()
    tool_repo = InMemoryCatalogRepository(ToolDefinition)
    workflow_repo = InMemoryWorkflowCatalogRepository()
    model_provider_repo = InMemoryCatalogRepository(ModelProviderDefinition)
    model_profile_repo = InMemoryModelProfileCatalogRepository()
    schedule_repo = InMemoryScheduleRepository()
    runtime_adapter_repo = InMemoryCatalogRepository(RuntimeAdapterDefinition)
    runtime_revision_repo = InMemoryCatalogRepository(RuntimeRevision)
    mcp_server_repo = InMemoryCatalogRepository(MCPServerDefinition)
    execution_store = InMemoryExecutionStore()
    llm_provider_registry = ModelProviderRegistry()
    mcp_registry = MCPClientRegistry()
    approval_manager = ApprovalManager(execution_store=execution_store)
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
        runtime_operations=runtime_operations,
    )
    context = ApiContext(
        agent_repo=agent_repo,
        conversation_repo=conversation_repo,
        channel_identity_mapping_repo=channel_identity_mapping_repo,
        conversation_message_repo=conversation_message_repo,
        conversation_approval_repo=conversation_approval_repo,
        memory_repo=memory_repo,
        main_agent_profile_repo=main_agent_profile_repo,
        conversation_event_broker=conversation_event_broker,
        credential_repo=credential_repo,
        user_repo=user_repo,
        tool_repo=tool_repo,
        workflow_repo=workflow_repo,
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

    execution_engine.set_memory_prompt_builder(SharedMemoryPromptBuilder(context))
    execution_engine.execution_completion_handler = _persist_run_summary
    crewai_adapter.execution_completion_handler = _persist_run_summary
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
    main_agent_profile_repo = SQLMainAgentProfileRepository(session_factory)
    conversation_event_broker = ConversationEventBroker()
    credential_repo = SQLCredentialRepository(session_factory)
    user_repo = InMemoryUserRepository()
    tool_repo = SQLToolRepository(session_factory)
    workflow_repo = SQLWorkflowRepository(session_factory)
    model_provider_repo = SQLModelProviderRepository(session_factory)
    model_profile_repo = SQLModelProfileRepository(session_factory)
    schedule_repo = SQLScheduleRepository(session_factory)
    runtime_adapter_repo = SQLRuntimeAdapterRepository(session_factory)
    runtime_revision_repo = SQLRuntimeRevisionRepository(session_factory)
    mcp_server_repo = SQLMCPServerRepository(session_factory)
    execution_store = SQLExecutionStore(session_factory)
    llm_provider_registry = ModelProviderRegistry()
    mcp_registry = MCPClientRegistry()
    approval_manager = ApprovalManager(execution_store=execution_store)
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
        runtime_operations=runtime_operations,
    )
    context = ApiContext(
        agent_repo=agent_repo,
        conversation_repo=conversation_repo,
        channel_identity_mapping_repo=channel_identity_mapping_repo,
        conversation_message_repo=conversation_message_repo,
        conversation_approval_repo=conversation_approval_repo,
        memory_repo=memory_repo,
        main_agent_profile_repo=main_agent_profile_repo,
        conversation_event_broker=conversation_event_broker,
        credential_repo=credential_repo,
        user_repo=user_repo,
        tool_repo=tool_repo,
        workflow_repo=workflow_repo,
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

    execution_engine.set_memory_prompt_builder(SharedMemoryPromptBuilder(context))
    execution_engine.execution_completion_handler = _persist_run_summary
    crewai_adapter.execution_completion_handler = _persist_run_summary
    return context
