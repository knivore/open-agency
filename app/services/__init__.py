from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "AgentToolResolver": (".agent_tools", "AgentToolResolver"),
    "AgentService": (".agents", "AgentService"),
    "ChannelIdentityMappingService": (".channel_identity", "ChannelIdentityMappingService"),
    "ConversationDailySummaryService": (".conversation_daily_summary", "ConversationDailySummaryService"),
    "ConversationNotFoundError": (".conversations", "ConversationNotFoundError"),
    "ConversationService": (".conversations", "ConversationService"),
    "CredentialService": (".credentials", "CredentialService"),
    "DocumentIngestionError": (".document_ingestion", "DocumentIngestionError"),
    "DocumentIngestionService": (".document_ingestion", "DocumentIngestionService"),
    "ExecutionService": (".executions", "ExecutionService"),
    "execution_process_manager": (".executions", "execution_process_manager"),
    "ExecutionRunSummaryService": (".execution_run_summary", "ExecutionRunSummaryService"),
    "IntegrationsRegistryService": (".integrations_registry", "IntegrationsRegistryService"),
    "MainAgentModelProfileRequiredError": (".main_agent_setup", "MainAgentModelProfileRequiredError"),
    "MainAgentSetupConfig": (".main_agent_setup", "MainAgentSetupConfig"),
    "MainAgentSetupInvalidError": (".main_agent_setup", "MainAgentSetupInvalidError"),
    "MainAgentSetupRequiredError": (".main_agent_setup", "MainAgentSetupRequiredError"),
    "MainAgentSetupService": (".main_agent_setup", "MainAgentSetupService"),
    "MainAgentWorkflowMonitorService": (".main_agent_workflow_monitor", "MainAgentWorkflowMonitorService"),
    "MemoryEmbeddingError": (".memory", "MemoryEmbeddingError"),
    "MemoryPermissionError": (".memory", "MemoryPermissionError"),
    "MemoryPolicyError": (".memory", "MemoryPolicyError"),
    "MemoryService": (".memory", "MemoryService"),
    "ModelCatalogService": (".models", "ModelCatalogService"),
    "ScheduleService": (".schedules", "ScheduleService"),
    "WorkflowValidationService": (".workflow_validation", "WorkflowValidationService"),
    "WorkflowService": (".workflows", "WorkflowService"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name, __name__)
    value = getattr(module, attribute)
    globals()[name] = value
    return value
