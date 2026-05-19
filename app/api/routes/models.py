from __future__ import annotations

import time
import urllib.parse
from fastapi import APIRouter, HTTPException, Request, status
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_current_user_if_present
from app.domain import ModelProfileDefinition, ModelProviderDefinition, ModelProviderType
from app.services import ModelCatalogService
from app.utils.oauth_pkce import OPENAI_CODEX_CLIENT_ID, OPENAI_CODEX_REDIRECT_URI, OAuthPKCEHandler
from ._crud import build_crud_router


def _oauth_profile_id(config: dict, auth_profile_id: Optional[str]) -> str:
    return auth_profile_id or config.get("default_oauth_profile_id") or "default"


def _oauth_profile(config: dict, auth_profile_id: str) -> dict:
    auth_profiles = config.get("auth_profiles")
    if isinstance(auth_profiles, dict):
        record = auth_profiles.get(auth_profile_id)
        if isinstance(record, dict):
            return dict(record)
    return {
        key: config[key]
        for key in (
            "access_token",
            "refresh_token",
            "expires_at",
            "auth_mode",
            "client_id",
            "redirect_uri",
            "account_id",
            "accountId",
        )
        if key in config
    }


def _store_oauth_profile(
        config: dict,
        auth_profile_id: str,
        *,
        profile_patch: dict,
        set_default: bool = False,
) -> dict:
    new_config = dict(config or {})
    auth_profiles = dict(new_config.get("auth_profiles") or {})
    current = dict(auth_profiles.get(auth_profile_id) or {})
    current.update(profile_patch)
    auth_profiles[auth_profile_id] = current
    new_config["auth_profiles"] = auth_profiles

    default_profile_id = new_config.get("default_oauth_profile_id")
    if set_default or not default_profile_id:
        default_profile_id = auth_profile_id
        new_config["default_oauth_profile_id"] = default_profile_id

    if auth_profile_id == default_profile_id:
        for key in (
            "access_token",
            "refresh_token",
            "expires_at",
            "auth_mode",
            "client_id",
            "redirect_uri",
            "account_id",
        ):
            if key in current:
                new_config[key] = current[key]

    return new_config


def create_models_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = ModelCatalogService(context)
    router = APIRouter()
    router.include_router(
        build_crud_router(
            prefix="/model-providers",
            tag="Model Providers",
            summary_name="Model Provider",
            repo=context.model_provider_repo,
            model_cls=ModelProviderDefinition,
            context=context,
            read_scopes=["models:read"],
            write_scopes=["models:write"],
        )
    )
    router.include_router(
        build_crud_router(
            prefix="/model-profiles",
            tag="Model Profiles",
            summary_name="Model Profile",
            repo=context.model_profile_repo,
            model_cls=ModelProfileDefinition,
            context=context,
            read_scopes=["models:read"],
            write_scopes=["models:write"],
        )
    )

    @router.get("/model-providers/{provider_id}/health", summary="Test Model Provider Health")
    async def test_model_provider_health(provider_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["models:write"])
        result = await service.test_model_provider(provider_id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Model Provider '{provider_id}' not found")
        return result

    @router.post("/model-providers/{provider_id}/test", summary="Test Model Provider")
    async def test_model_provider(provider_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["models:write"])
        result = await service.test_model_provider(provider_id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Model Provider '{provider_id}' not found")
        return result

    @router.get("/model-providers/{provider_id}/models", summary="List Model Provider Models")
    async def list_model_provider_models(provider_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["models:read"])
        result = await service.list_model_provider_models(provider_id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail=f"Model Provider '{provider_id}' not found")
        return result

    @router.get("/model-profiles/{profile_id}/health", summary="Test Model Profile Health")
    async def test_model_profile_health(profile_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["models:write"])
        result = await service.test_model_profile(profile_id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Model Profile '{profile_id}' not found")
        return result

    @router.post("/model-profiles/{profile_id}/test", summary="Test Model Profile")
    async def test_model_profile(profile_id: str, request: Request):
        await resolve_current_user_if_present(request, context, required_scopes=["models:write"])
        result = await service.test_model_profile(profile_id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Model Profile '{profile_id}' not found")
        return result

    @router.post("/model-providers/{provider_id}/authorize", summary="Initiate OAuth PKCE Flow")
    async def authorize_model_provider(
            provider_id: str,
            request: Request,
            client_id: Optional[str] = None,
            auth_profile_id: Optional[str] = None,
    ):
        await resolve_current_user_if_present(request, context, required_scopes=["models:write"])
        provider = await context.model_provider_repo.get(provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        supported_providers = {
            ModelProviderType.OPENAI_CODEX,
            ModelProviderType.GOOGLE,
            ModelProviderType.AZURE_OPENAI
        }
        if provider.provider_type not in supported_providers:
            raise HTTPException(
                status_code=400,
                detail=f"OAuth flow only supported for: {', '.join([p.value for p in supported_providers])}"
            )

        config = provider.config or {}
        client_id = client_id or config.get("client_id") or config.get("clientId")
        if client_id == "DEFAULT_CLIENT_ID":
            client_id = None

        profile_id = _oauth_profile_id(config, auth_profile_id)
        profile_config = _oauth_profile(config, profile_id)

        redirect_uri = (
            config.get("redirect_uri")
            or config.get("redirectUri")
            or profile_config.get("redirect_uri")
            or (
                OPENAI_CODEX_REDIRECT_URI
                if provider.provider_type == ModelProviderType.OPENAI_CODEX
                else "http://127.0.0.1:1455/auth/callback"
            )
        )
        tenant_id = config.get("tenant_id") or config.get("tenantId")

        handler = OAuthPKCEHandler.for_provider(
            provider.provider_type.value,
            client_id=client_id or "",
            redirect_uri=redirect_uri,
            tenant_id=tenant_id
        )
        handler.generate_pkce_data()
        auth_url = handler.get_authorization_url()

        new_config = _store_oauth_profile(
            config,
            profile_id,
            profile_patch={
                "client_id": handler.client_id,
                "redirect_uri": handler.redirect_uri,
                "auth_mode": "chatgpt",
                "pending_state": handler.state,
                "pending_pkce_verifier": handler.code_verifier,
                "pending_created_at": time.time(),
            },
            set_default=True,
        )
        await context.model_provider_repo.update(provider_id, {"config": new_config})

        # Start the callback server in the background if it's a loopback redirect.
        if handler.redirect_uri and ("localhost" in handler.redirect_uri or "127.0.0.1" in handler.redirect_uri):
            # Extract port if possible, default to 1455.
            port = 1455
            try:
                parsed = urllib.parse.urlparse(handler.redirect_uri)
                if parsed.port:
                    port = parsed.port
            except:
                pass
            handler.start_callback_server(port=port)

        return {
            "auth_url": auth_url,
            "message": "Open the auth_url in your browser to complete authorization.",
            "pkce_verifier": handler.code_verifier,
            "client_id": handler.client_id,
            "state": handler.state,
            "redirect_uri": handler.redirect_uri,
            "auth_profile_id": profile_id,
        }

    @router.post("/model-providers/{provider_id}/callback-complete", summary="Complete OAuth PKCE Flow")
    async def complete_authorize_model_provider(
            provider_id: str,
            request: Request,
            code: Optional[str] = None,
            pkce_verifier: Optional[str] = None,
            client_id: Optional[str] = None,
            state: Optional[str] = None,
            redirect_url: Optional[str] = None,
            auth_profile_id: Optional[str] = None,
    ):
        await resolve_current_user_if_present(request, context, required_scopes=["models:write"])
        provider = await context.model_provider_repo.get(provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail="Provider not found")

        config = provider.config or {}
        profile_id = _oauth_profile_id(config, auth_profile_id)
        profile_config = _oauth_profile(config, profile_id)

        if redirect_url:
            parsed = OAuthPKCEHandler.parse_redirect_url(redirect_url)
            if parsed.get("error"):
                raise HTTPException(status_code=400, detail=parsed.get("error_description") or parsed["error"])
            code = code or parsed.get("code")
            state = state or parsed.get("state")

        client_id = client_id or profile_config.get("client_id") or config.get("client_id") or config.get("clientId")
        if client_id == "DEFAULT_CLIENT_ID":
            client_id = None

        redirect_uri = (
            config.get("redirect_uri")
            or config.get("redirectUri")
            or profile_config.get("redirect_uri")
            or (
                OPENAI_CODEX_REDIRECT_URI
                if provider.provider_type == ModelProviderType.OPENAI_CODEX
                else "http://127.0.0.1:1455/auth/callback"
            )
        )
        tenant_id = config.get("tenant_id") or config.get("tenantId")
        expected_state = profile_config.get("pending_state")
        if expected_state and state != expected_state:
            raise HTTPException(status_code=400, detail="OAuth state mismatch")
        pkce_verifier = pkce_verifier or profile_config.get("pending_pkce_verifier")
        if not code:
            raise HTTPException(status_code=400, detail="Authorization code is required")
        if not pkce_verifier:
            raise HTTPException(status_code=400, detail="PKCE verifier is required")

        handler = OAuthPKCEHandler.for_provider(
            provider.provider_type.value,
            client_id=client_id or "",
            redirect_uri=redirect_uri,
            tenant_id=tenant_id
        )
        tokens = await handler.exchange_token(code, pkce_verifier)
        account_id = OAuthPKCEHandler.extract_account_id(tokens.get("access_token"))

        # Store tokens in provider config
        new_config = _store_oauth_profile(
            provider.config or {},
            profile_id,
            profile_patch={
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token"),
                "expires_at": time.time() + tokens.get("expires_in", 3600),
                "auth_mode": "chatgpt",
                "client_id": handler.client_id,
                "redirect_uri": handler.redirect_uri,
                "account_id": account_id,
            },
            set_default=True,
        )
        auth_profiles = dict(new_config.get("auth_profiles") or {})
        active_profile = dict(auth_profiles.get(profile_id) or {})
        active_profile.pop("pending_state", None)
        active_profile.pop("pending_pkce_verifier", None)
        active_profile.pop("pending_created_at", None)
        auth_profiles[profile_id] = active_profile
        new_config["auth_profiles"] = auth_profiles

        await context.model_provider_repo.update(provider_id, {"config": new_config})

        return {
            "status": "success",
            "message": "Tokens stored successfully",
            "auth_profile_id": profile_id,
            "account_id": account_id,
        }

    @router.post("/model-providers/{provider_id}/device-authorize", summary="Initiate OAuth Device Flow")
    async def device_authorize_model_provider(
            provider_id: str,
            request: Request,
            auth_profile_id: Optional[str] = None,
    ):
        await resolve_current_user_if_present(request, context, required_scopes=["models:write"])
        provider = await context.model_provider_repo.get(provider_id)
        if not provider or provider.provider_type != ModelProviderType.OPENAI_CODEX:
            raise HTTPException(status_code=400, detail="Device flow only supported for OpenAI Codex")

        config = provider.config or {}
        profile_id = _oauth_profile_id(config, auth_profile_id)
        profile_config = _oauth_profile(config, profile_id)
        client_id = profile_config.get("client_id") or config.get("client_id") or config.get("clientId")
        if not client_id or client_id == "DEFAULT_CLIENT_ID":
            client_id = OPENAI_CODEX_CLIENT_ID

        handler = OAuthPKCEHandler.for_provider(
            provider.provider_type.value,
            client_id=client_id
        )
        
        device_data = await handler.initiate_device_auth()
        
        return {
            "device_code": device_data["device_code"],
            "user_code": device_data["user_code"],
            "verification_uri": device_data["verification_uri"],
            "expires_in": device_data["expires_in"],
            "interval": device_data.get("interval", 5),
            "message": f"Open {device_data['verification_uri']} and enter code: {device_data['user_code']}",
            "auth_profile_id": profile_id,
        }

    @router.post("/model-providers/{provider_id}/device-complete", summary="Complete OAuth Device Flow")
    async def complete_device_authorize_model_provider(
            provider_id: str,
            request: Request,
            device_code: str,
            auth_profile_id: Optional[str] = None,
    ):
        await resolve_current_user_if_present(request, context, required_scopes=["models:write"])
        provider = await context.model_provider_repo.get(provider_id)
        if not provider or provider.provider_type != ModelProviderType.OPENAI_CODEX:
            raise HTTPException(status_code=400, detail="Device flow only supported for OpenAI Codex")

        config = provider.config or {}
        profile_id = _oauth_profile_id(config, auth_profile_id)
        profile_config = _oauth_profile(config, profile_id)
        client_id = profile_config.get("client_id") or config.get("client_id") or config.get("clientId")
        if not client_id or client_id == "DEFAULT_CLIENT_ID":
            client_id = OPENAI_CODEX_CLIENT_ID

        handler = OAuthPKCEHandler.for_provider(
            provider.provider_type.value,
            client_id=client_id
        )
        
        tokens = await handler.poll_device_token(device_code)
        account_id = OAuthPKCEHandler.extract_account_id(tokens.get("access_token"))

        # Store tokens in provider config
        new_config = _store_oauth_profile(
            provider.config or {},
            profile_id,
            profile_patch={
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token"),
                "expires_at": time.time() + tokens.get("expires_in", 3600),
                "auth_mode": "chatgpt",
                "client_id": handler.client_id,
                "redirect_uri": handler.redirect_uri,
                "account_id": account_id,
            },
            set_default=True,
        )

        await context.model_provider_repo.update(provider_id, {"config": new_config})

        return {
            "status": "success",
            "message": "Tokens stored successfully",
            "auth_profile_id": profile_id,
            "account_id": account_id,
        }

    return router
