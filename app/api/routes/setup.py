"""Backend-owned onboarding endpoints for the beginner-friendly setup flow."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
import httpx
from pydantic import BaseModel, Field
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user
from app.services.openvoice_setup import OpenVoiceSetupService
from app.services.public_endpoints import PublicEndpointService
from app.services.setup_onboarding import SetupOnboardingService
from app.services.tunnel_preferences import TunnelPreferenceService, TunnelProvider
from app.tools.implementations.voice import generate_voice


class SetupModelProfileRequest(BaseModel):
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None


class SetupMainAgentRequest(BaseModel):
    model_profile_id: str
    agent_name: str = Field(default="Main Agent", min_length=1)


class SetupRecommendedAgentsRequest(BaseModel):
    include_coder: bool = True
    include_embedding: bool = True
    include_evaluation: bool = True


class SetupTunnelPreferenceRequest(BaseModel):
    provider: TunnelProvider
    custom_domain: str | None = None


class SetupOpenVoiceSettingsRequest(BaseModel):
    default_voice: str


class SetupOpenVoiceInstallRequest(BaseModel):
    force: bool = False


class SetupOpenVoiceTestRequest(BaseModel):
    text: str = Field(
        default="Hello. This is your Agency OpenVoice test using the selected built-in voice.",
        min_length=1,
        max_length=500,
    )


def create_setup_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = SetupOnboardingService(context)
    tunnel_service = TunnelPreferenceService()
    endpoint_service = PublicEndpointService(context)
    openvoice_service = OpenVoiceSetupService()
    router = APIRouter(prefix="/setup", tags=["Setup"])

    async def tunnel_preference_payload():
        preference = tunnel_service.get()
        return {
            **preference.model_dump(mode="json"),
            "current_public_url": await endpoint_service.get_current_webhook_base_url(),
            "requirements": tunnel_service.requirements(preference),
        }

    @router.get("/tunnel-preference", summary="Get Saved Tunnel Preference")
    async def get_tunnel_preference():
        return await tunnel_preference_payload()

    @router.put("/tunnel-preference", summary="Save Tunnel Preference")
    async def save_tunnel_preference(payload: SetupTunnelPreferenceRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["integrations:write"])
        try:
            tunnel_service.save(
                provider=payload.provider,
                custom_domain=payload.custom_domain,
                source="browser",
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return await tunnel_preference_payload()

    @router.post("/model-profile", summary="Create Setup Model Profile")
    async def create_setup_model_profile(payload: SetupModelProfileRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["models:write"])
        try:
            profile = await service.ensure_model_profile(
                provider_key=payload.provider,
                model_name=payload.model,
                api_key=payload.api_key,
                base_url=payload.base_url,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return profile.model_dump(mode="json")

    @router.post("/main-agent", summary="Create Setup Main Agent")
    async def create_setup_main_agent(payload: SetupMainAgentRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["models:write"])
        try:
            profile = await service.ensure_main_agent(
                model_profile_id=payload.model_profile_id,
                agent_name=payload.agent_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return profile.model_dump(mode="json")

    @router.post("/recommended-agents", summary="Create Recommended Setup Agents")
    async def create_setup_recommended_agents(payload: SetupRecommendedAgentsRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["agents:write"])
        try:
            result = await service.ensure_recommended_agents(
                include_coder=payload.include_coder,
                include_embedding=payload.include_embedding,
                include_evaluation=payload.include_evaluation,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return {
            "coder_agent_id": result.coder_agent_id,
            "embedding_agent_id": result.embedding_agent_id,
            "embedding_model_profile_id": result.embedding_model_profile_id,
            "evaluation_agent_id": result.evaluation_agent_id,
        }

    @router.get("/openvoice", summary="Get Optional OpenVoice Setup Status")
    async def get_openvoice_status(request: Request):
        await resolve_current_user(request, context, required_scopes=["tools:read"])
        return openvoice_service.status()

    @router.put("/openvoice", summary="Update Optional OpenVoice Settings")
    async def update_openvoice_settings(payload: SetupOpenVoiceSettingsRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["tools:write"])
        try:
            openvoice_service.save_settings(default_voice=payload.default_voice)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        return openvoice_service.status()

    @router.post("/openvoice/install", summary="Install Or Repair Optional OpenVoice Checkpoints")
    async def install_openvoice(payload: SetupOpenVoiceInstallRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["tools:write"])
        try:
            return await openvoice_service.install_checkpoints(force=payload.force)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"OpenVoice checkpoint download failed: {exc.__class__.__name__}",
            ) from exc

    @router.post("/openvoice/test", summary="Generate Optional OpenVoice Test Audio")
    async def test_openvoice(payload: SetupOpenVoiceTestRequest, request: Request):
        await resolve_current_user(request, context, required_scopes=["tools:write"])
        current = openvoice_service.status()
        if not current["ready"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="OpenVoice is not ready. Install or repair the checkpoints first.",
            )
        voice = str(current["settings"]["default_voice"])
        # Diagnostic samples must be repeatable; Agency storage rejects an
        # existing key instead of silently overwriting a prior voice artifact.
        output_name = f"openvoice-profile-test-{uuid4().hex}.wav"
        result = await asyncio.to_thread(
            generate_voice,
            text=payload.text,
            provider="openvoice_local",
            voice=voice,
            output_name=output_name,
            storage_key_prefix="voice/diagnostics",
            purpose="Profile OpenVoice configuration test.",
            ai_disclosure=True,
            consent_confirmed=False,
            dry_run=False,
            metadata={"language": "English", "source": "profile_openvoice_test"},
        )
        file_path = result.get("file_path")
        if result.get("status") != "generated" or not isinstance(file_path, str):
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=result)
        audio_bytes = Path(file_path).read_bytes()
        return {
            "result": result,
            "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
            "content_type": result.get("content_type") or "audio/wav",
        }

    return router
