"""Read-only integration and connector capability registry routes."""

from __future__ import annotations

from fastapi import APIRouter
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.domain import ConnectorCapabilitiesPayload, IntegrationRegistryPayload
from app.services.integrations_registry import IntegrationsRegistryService


def create_integrations_registry_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = IntegrationsRegistryService()
    router = APIRouter(prefix="/integrations", tags=["Integrations Registry"])

    @router.get("/categories", response_model=IntegrationRegistryPayload,
                summary="List Integration Registry Categories")
    async def list_integration_categories() -> IntegrationRegistryPayload:
        return service.list_categories()

    @router.get("/connectors/capabilities", response_model=ConnectorCapabilitiesPayload,
                summary="List Connector Capabilities")
    async def list_connector_capabilities() -> ConnectorCapabilitiesPayload:
        return service.list_connector_capabilities()

    return router
