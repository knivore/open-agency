"""Local bootstrap and password-auth routes for first-run onboarding."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from typing import Optional

from app.api.context import ApiContext, get_default_api_context
from app.api.identity import resolve_user_from_bearer_token
from app.services.local_auth import (
    LocalAuthBootstrapUnavailableError,
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
        result = await service.authenticate(email=payload.email, password=payload.password)
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

    return router
