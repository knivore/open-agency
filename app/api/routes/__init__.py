"""Route composition for the public backend API surface.

Individual route modules keep ownership of request/response behavior. This file
only wires those routers into one `APIRouter` so tests, the app factory, and docs
checks can reason about the complete registered route table.
"""

from __future__ import annotations

from fastapi import APIRouter
from typing import Optional

from app.api.context import ApiContext
from app.api.streaming.graph_sse import create_graph_stream_router
from app.api.streaming.runtime_sse import create_runtime_sse_router
from app.api.websocket.runtime_ws import create_runtime_websocket_router
from app.modules.registry import optional_module_route_factories
from app.protocols.a2a.routes import create_a2a_router
from .agents import create_agents_router
from .api_tokens import create_api_tokens_router
from .auth import create_auth_router
from .capabilities import create_capabilities_router
from .connector_installations import create_connector_installations_router
from .connectors import create_connectors_router
from .conversations.channels import create_conversation_channels_router
from .conversations.core import create_conversations_router
from .credentials import create_credentials_router
from .documents import create_documents_router
from .executions import create_executions_router
from .goals import create_goals_router
from .graph_projection import create_graph_projection_router
from .graph_read import create_graph_read_router
from .health import create_health_router
from .integrations_registry import create_integrations_registry_router
from .main_agent_monitor import create_main_agent_monitor_router
from .marketplace import create_marketplace_router
from .mcp import create_mcp_router
from .memory import create_memory_router
from .models import create_models_router
from .observability import create_observability_router
from .onecli import create_onecli_router
from .persona import create_persona_router
from .runtime_adapters import create_runtime_adapters_router
from .schedules import create_schedules_router
from .setup import create_setup_router
from .speech import create_speech_router
from .storage import create_storage_router
from .tool_contracts import create_tool_contracts_router
from .tool_runtime import create_tool_runtime_router
from .tools import create_tools_router
from .users import create_users_router
from .vision import create_vision_router
from .workflow_builder import create_workflow_builder_router
from .workflows import create_workflows_router


def create_api_router(context: Optional[ApiContext] = None) -> APIRouter:
    """Register all API, SSE, WebSocket, and protocol routers."""
    router = APIRouter()
    router.include_router(create_health_router(context))
    router.include_router(create_auth_router(context))
    router.include_router(create_setup_router(context))
    router.include_router(create_capabilities_router())
    router.include_router(create_agents_router(context))
    router.include_router(create_conversations_router(context))
    router.include_router(create_conversation_channels_router(context))
    router.include_router(create_users_router(context))
    router.include_router(create_goals_router(context))
    router.include_router(create_api_tokens_router(context))
    router.include_router(create_credentials_router(context))
    for create_optional_module_router in optional_module_route_factories():
        router.include_router(create_optional_module_router(context))
    router.include_router(create_connectors_router(context))
    router.include_router(create_connector_installations_router(context))
    router.include_router(create_integrations_registry_router(context))
    router.include_router(create_tool_contracts_router(context))
    router.include_router(create_tool_runtime_router(context))
    router.include_router(create_tools_router(context))
    router.include_router(create_vision_router(context))
    router.include_router(create_speech_router(context))
    router.include_router(create_models_router(context))
    router.include_router(create_onecli_router(context))
    router.include_router(create_marketplace_router(context))
    router.include_router(create_memory_router(context))
    router.include_router(create_documents_router(context))
    router.include_router(create_mcp_router(context))
    router.include_router(create_main_agent_monitor_router(context))
    router.include_router(create_schedules_router(context))
    router.include_router(create_persona_router(context))
    router.include_router(create_storage_router(context))
    router.include_router(create_runtime_adapters_router(context))
    router.include_router(create_workflow_builder_router(context))
    router.include_router(create_workflows_router(context))
    router.include_router(create_executions_router(context))
    router.include_router(create_graph_projection_router(context))
    router.include_router(create_graph_read_router(context))
    router.include_router(create_a2a_router(context))
    router.include_router(create_observability_router(context))
    router.include_router(create_graph_stream_router(context))
    router.include_router(create_runtime_sse_router(context))
    router.include_router(create_runtime_websocket_router(context))
    return router


__all__ = [
    "create_api_router",
    "create_a2a_router",
    "create_agents_router",
    "create_api_tokens_router",
    "create_auth_router",
    "create_capabilities_router",
    "create_conversation_channels_router",
    "create_conversations_router",
    "create_connectors_router",
    "create_connector_installations_router",
    "create_credentials_router",
    "create_documents_router",
    "create_executions_router",
    "create_graph_projection_router",
    "create_graph_read_router",
    "create_graph_stream_router",
    "create_goals_router",
    "create_health_router",
    "create_integrations_registry_router",
    "create_marketplace_router",
    "create_main_agent_monitor_router",
    "create_memory_router",
    "create_mcp_router",
    "create_models_router",
    "create_onecli_router",
    "create_observability_router",
    "create_runtime_adapters_router",
    "create_runtime_sse_router",
    "create_runtime_websocket_router",
    "create_schedules_router",
    "create_setup_router",
    "create_persona_router",
    "create_setup_router",
    "create_storage_router",
    "create_tools_router",
    "create_tool_contracts_router",
    "create_tool_runtime_router",
    "create_users_router",
    "create_vision_router",
    "create_speech_router",
    "create_workflow_builder_router",
    "create_workflows_router",
]
