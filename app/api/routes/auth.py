"""Local bootstrap and password-auth routes for first-run onboarding."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_user_from_bearer_token
from app.services.local_auth import (
    LocalAuthBootstrapUnavailableError,
    LocalAuthCredentialsUnavailableError,
    LocalAuthCurrentPasswordError,
    LocalAuthEmailConflictError,
    LocalAuthRateLimitError,
    LocalAuthService,
)
from ._user_payload import public_user_payload


class AuthBootstrapRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    display_name: str | None = Field(default=None, max_length=200)


class AuthLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class AuthCredentialsUpdateRequest(BaseModel):
    email: EmailStr
    current_password: str = Field(min_length=1)
    new_password: str | None = Field(default=None, min_length=8)


def create_auth_router(context: Optional[ApiContext] = None) -> APIRouter:
    context = context or get_default_api_context()
    service = LocalAuthService(context)
    router = APIRouter(prefix="/auth", tags=["Auth"])

    @router.post("/bootstrap", summary="Create First Local Admin")
    async def bootstrap_local_admin(payload: AuthBootstrapRequest):
        try:
            result = await service.bootstrap_local_admin(
                email=payload.email,
                password=payload.password,
                display_name=payload.display_name,
            )
        except LocalAuthBootstrapUnavailableError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

        return {
            "bootstrap_complete": True,
            "user": public_user_payload(result.user),
        }

    @router.post("/login", summary="Login With Local Credentials")
    async def login(payload: AuthLoginRequest):
        try:
            result = await service.authenticate(email=payload.email, password=payload.password)
        except LocalAuthRateLimitError as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(exc),
                headers={"Retry-After": "60"},
            ) from exc
        if result is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
        return {
            "access_token": result.raw_token,
            "token_type": "bearer",
            "user": public_user_payload(result.user),
        }

    @router.get("/me", summary="Get Current Authenticated User")
    async def me(request: Request):
        user = await resolve_user_from_bearer_token(request, context)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        return public_user_payload(user)

    @router.patch("/me/credentials", summary="Update Local Sign-In Credentials")
    async def update_credentials(payload: AuthCredentialsUpdateRequest, request: Request):
        # This sensitive operation requires the actual local bearer session, not
        # trusted identity headers alone, in addition to current-password proof.
        user = await resolve_user_from_bearer_token(request, context)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        try:
            result = await service.update_credentials(
                user=user,
                current_password=payload.current_password,
                email=payload.email,
                new_password=payload.new_password,
            )
        except LocalAuthCurrentPasswordError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        except LocalAuthCredentialsUnavailableError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except LocalAuthEmailConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

        return {
            "user": public_user_payload(result.user),
            "reauthentication_required": True,
            "revoked_sessions": result.revoked_sessions,
        }

    return router
