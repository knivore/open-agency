"""Request identity and API-token resolution helpers.

Routes use this module to validate trusted frontend identity headers, bearer
tokens, required scopes, and disabled-user checks without embedding auth policy
inside each endpoint.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, Request, status
from typing import Iterable

from app.api.context import ApiContext
from app.core.config import get_settings
from app.domain import API_TOKEN_SCOPE_DEFINITIONS, ApiTokenDefinition, UserDefinition

logger = logging.getLogger(__name__)

API_TOKEN_LAST_USED_WRITE_INTERVAL = timedelta(minutes=5)


def require_active_user(user: UserDefinition) -> None:
    status_value = getattr(user.status, "value", user.status)
    if status_value == "disabled":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Current user is disabled")


def header_value(request: Request, name: str) -> str | None:
    value = request.headers.get(name)
    return value.strip() if value and value.strip() else None


def require_trusted_identity_source(request: Request) -> None:
    settings = get_settings()
    configured_key = settings.agency_internal_api_key
    provided_key = header_value(request, "x-agency-internal-api-key")

    if configured_key:
        if provided_key != configured_key:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Trusted identity source is required")
        return

    if settings.app_env == "test":
        return

    # Identity headers are bearer-equivalent assertions. Development mode may
    # be tunneled publicly, so only tests may use them without a shared secret.
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Trusted identity source is required")


def hash_bearer_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(request: Request) -> str | None:
    authorization = header_value(request, "authorization")
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def request_has_identity(request: Request) -> bool:
    return bool(
        bearer_token(request)
        or header_value(request, "x-agency-user-id")
        or header_value(request, "x-agency-user-email")
    )


def _is_privileged_management_scope(scope: str) -> bool:
    _, _, action = scope.partition(":")
    return action in {"write", "run", "admin"}


def _record_management_console_action(
        request: Request,
        context: ApiContext,
        user: UserDefinition,
        required_scopes: Iterable[str] | None,
        *,
        identity_mode: str,
) -> None:
    client_name = header_value(request, "x-agency-client")
    if client_name != "agency-fe":
        return
    scopes = [scope for scope in (required_scopes or []) if scope]
    if request.method.upper() in {"GET", "HEAD", "OPTIONS"} and not any(
            _is_privileged_management_scope(scope) for scope in scopes
    ):
        return
    if getattr(request.state, "management_console_action_logged", False):
        return

    request.state.management_console_action_logged = True
    context.runtime_operations.record_action(
        "management_console.privileged_request",
        actor_user_id=user.id,
        actor_user_email=user.email,
        client=client_name,
        identity_mode=identity_mode,
        method=request.method,
        path=request.url.path,
        required_scopes=scopes,
    )


def _record_authorization_failure(
        request: Request,
        context: ApiContext,
        *,
        reason: str,
        status_code: int,
        required_scopes: Iterable[str] | None = None,
        missing_scopes: Iterable[str] | None = None,
) -> None:
    context.runtime_operations.record_action(
        "authorization.failure",
        reason=reason,
        status_code=status_code,
        method=request.method,
        path=request.url.path,
        client=header_value(request, "x-agency-client"),
        required_scopes=[scope for scope in (required_scopes or []) if scope],
        missing_scopes=[scope for scope in (missing_scopes or []) if scope],
        has_bearer_token=bool(bearer_token(request)),
        has_trusted_identity_headers=bool(
            header_value(request, "x-agency-user-id") or header_value(request, "x-agency-user-email")
        ),
    )


def is_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def _should_record_token_usage(token: ApiTokenDefinition, used_at: datetime) -> bool:
    last_used_at = token.last_used_at
    if last_used_at is None:
        return True
    if last_used_at.tzinfo is None:
        last_used_at = last_used_at.replace(tzinfo=timezone.utc)
    return used_at - last_used_at >= API_TOKEN_LAST_USED_WRITE_INTERVAL


async def _record_token_usage_best_effort(
        context: ApiContext,
        token: ApiTokenDefinition,
        used_at: datetime,
) -> None:
    if not _should_record_token_usage(token, used_at):
        return
    try:
        # This timestamp is operational metadata, not part of the authorization
        # decision. Throttling avoids a database write on every authenticated read,
        # and a storage-pressure failure must not turn a valid token into a 500.
        await context.api_token_repo.update(token.id, {"last_used_at": used_at})
    except Exception as exc:
        logger.warning(
            "Unable to update last_used_at for API token '%s'; authentication will continue: %s",
            token.id,
            exc,
        )


async def resolve_bearer_token_auth(
        request: Request, context: ApiContext
) -> tuple[ApiTokenDefinition, UserDefinition] | None:
    raw_token = bearer_token(request)
    if raw_token is None or not hasattr(context, "api_token_repo"):
        return None
    token = await context.api_token_repo.find_by_hash(hash_bearer_token(raw_token))
    if token is None or token.revoked_at is not None or is_expired(token.expires_at):
        _record_authorization_failure(
            request,
            context,
            reason="invalid_or_revoked_api_token",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked API token")

    if token.metadata.get("issued_by") == "local_auth" and token.metadata.get("session") is True:
        # Local UI sessions intentionally receive the complete catalog. Refresh them on use so
        # adding a new first-party scope does not strand already signed-in administrators.
        catalog_scopes = [scope.id for scope in API_TOKEN_SCOPE_DEFINITIONS]
        refreshed_scopes = list(dict.fromkeys([*token.scopes, *catalog_scopes]))
        if refreshed_scopes != token.scopes:
            refreshed_token = await context.api_token_repo.update(token.id, {"scopes": refreshed_scopes})
            if refreshed_token is not None:
                token = refreshed_token

    user = await context.user_repo.get(token.owner_user_id)
    if user is None:
        _record_authorization_failure(
            request,
            context,
            reason="api_token_owner_unavailable",
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API token owner is unavailable")
    require_active_user(user)
    used_at = datetime.now(timezone.utc)
    await _record_token_usage_best_effort(context, token, used_at)
    context.runtime_operations.record_action(
        "api_token.used",
        token_id=token.id,
        owner_user_id=token.owner_user_id,
        scopes=token.scopes,
        prefix=token.prefix,
        last4=token.last4,
        path=request.url.path,
        method=request.method,
        used_at=used_at.isoformat(),
    )
    return token, user


def _require_token_scopes(token: ApiTokenDefinition, required_scopes: Iterable[str]) -> None:
    required = {scope.strip() for scope in required_scopes if scope.strip()}
    if not required:
        return
    granted = set(token.scopes)
    if not required.issubset(granted):
        missing = sorted(required.difference(granted))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": "API token does not grant the required scopes.",
                "missingScopes": missing,
                "grantedScopes": sorted(granted),
            },
        )


async def resolve_user_from_bearer_token(request: Request, context: ApiContext) -> UserDefinition | None:
    resolved = await resolve_bearer_token_auth(request, context)
    if resolved is None:
        return None
    _, user = resolved
    return user


async def resolve_current_user(
        request: Request,
        context: ApiContext,
        required_scopes: Iterable[str] | None = None,
) -> UserDefinition:
    has_trusted_identity_headers = bool(
        header_value(request, "x-agency-user-id") or header_value(request, "x-agency-user-email")
    )

    try:
        bearer_auth = await resolve_bearer_token_auth(request, context)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_401_UNAUTHORIZED or not has_trusted_identity_headers:
            raise
        bearer_auth = None

    if bearer_auth is not None:
        token, bearer_user = bearer_auth
        if required_scopes is not None:
            try:
                _require_token_scopes(token, required_scopes)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                _record_authorization_failure(
                    request,
                    context,
                    reason="api_token_scope_missing",
                    status_code=exc.status_code,
                    required_scopes=required_scopes,
                    missing_scopes=detail.get("missingScopes", []),
                )
                raise
        _record_management_console_action(
            request,
            context,
            bearer_user,
            required_scopes,
            identity_mode="bearer_token",
        )
        request.state.authenticated_api_token = token
        return bearer_user

    try:
        require_trusted_identity_source(request)
    except HTTPException as exc:
        _record_authorization_failure(
            request,
            context,
            reason="trusted_identity_source_required",
            status_code=exc.status_code,
            required_scopes=required_scopes,
        )
        raise

    user_id = header_value(request, "x-agency-user-id")
    request.state.authenticated_api_token = None
    if user_id:
        user = await context.user_repo.get(user_id)
        if user is not None:
            require_active_user(user)
            _record_management_console_action(
                request,
                context,
                user,
                required_scopes,
                identity_mode="trusted_identity_headers",
            )
            return user

    email = header_value(request, "x-agency-user-email")
    if email:
        user = await context.user_repo.find_by_email(email)
        if user is not None:
            require_active_user(user)
            _record_management_console_action(
                request,
                context,
                user,
                required_scopes,
                identity_mode="trusted_identity_headers",
            )
            return user
        _record_authorization_failure(
            request,
            context,
            reason="current_user_not_synced",
            status_code=status.HTTP_404_NOT_FOUND,
            required_scopes=required_scopes,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Current user has not been synced")

    _record_authorization_failure(
        request,
        context,
        reason="current_user_identity_required",
        status_code=status.HTTP_401_UNAUTHORIZED,
        required_scopes=required_scopes,
    )
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Current user identity is required")


async def resolve_current_user_if_present(
        request: Request,
        context: ApiContext,
        required_scopes: Iterable[str] | None = None,
) -> UserDefinition | None:
    if not request_has_identity(request):
        return None
    try:
        return await resolve_current_user(request, context, required_scopes=required_scopes)
    except HTTPException as exc:
        # Optional-auth routes should remain usable when callers attach a non-Agency
        # bearer token (for example, an upstream app session token).
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return None
        raise
