from __future__ import annotations

from fastapi import APIRouter
from typing import Optional

from app.api.context import ApiContext
from .a2a import create_a2a_router
from .agents import create_agents_router
from .capabilities import create_capabilities_router
from .conversations import create_conversation_channels_router, create_conversations_router
from .credentials import create_credentials_router
from .documents import create_documents_router
from .executions import create_executions_router
from .health import create_health_router
from .integrations_registry import create_integrations_registry_router
from .mcp import create_mcp_router
from .models import create_models_router
from .observability import create_observability_router
from .runtime_adapters import create_runtime_adapters_router
from .schedules import create_schedules_router
from .storage import create_storage_router
from .tools import create_tools_router
from .tool_contracts import create_tool_contracts_router
from .tool_runtime import create_tool_runtime_router
from .voice import create_voice_router
from .workflow_builder import create_workflow_builder_router
from .workflows import create_workflows_router
from app.api.streaming import create_runtime_sse_router
from app.api.websocket import create_runtime_websocket_router


def create_api_router(context: Optional[ApiContext] = None) -> APIRouter:
    router = APIRouter()
    router.include_router(create_health_router())
    router.include_router(create_capabilities_router())
    router.include_router(create_agents_router(context))
    router.include_router(create_conversations_router(context))
    router.include_router(create_conversation_channels_router(context))
    router.include_router(create_credentials_router(context))
    router.include_router(create_integrations_registry_router(context))
    router.include_router(create_tool_contracts_router(context))
    router.include_router(create_tool_runtime_router(context))
    router.include_router(create_tools_router(context))
    router.include_router(create_voice_router(context))
    router.include_router(create_models_router(context))
    router.include_router(create_documents_router(context))
    router.include_router(create_mcp_router(context))
    router.include_router(create_schedules_router(context))
    router.include_router(create_storage_router())
    router.include_router(create_runtime_adapters_router(context))
    router.include_router(create_workflow_builder_router(context))
    router.include_router(create_workflows_router(context))
    router.include_router(create_executions_router(context))
    router.include_router(create_a2a_router(context))
    router.include_router(create_observability_router(context))
    router.include_router(create_runtime_sse_router())
    router.include_router(create_runtime_websocket_router())
    return router


__all__ = [
    "create_api_router",
    "create_a2a_router",
    "create_agents_router",
    "create_capabilities_router",
    "create_conversation_channels_router",
    "create_conversations_router",
    "create_credentials_router",
    "create_documents_router",
    "create_executions_router",
    "create_health_router",
    "create_integrations_registry_router",
    "create_mcp_router",
    "create_models_router",
    "create_observability_router",
    "create_runtime_adapters_router",
    "create_runtime_sse_router",
    "create_runtime_websocket_router",
    "create_schedules_router",
    "create_storage_router",
    "create_tools_router",
    "create_tool_contracts_router",
    "create_tool_runtime_router",
    "create_voice_router",
    "create_workflow_builder_router",
    "create_workflows_router",
]
