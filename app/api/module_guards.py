"""Route guards for optional backend modules."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.modules.registry import optional_module_available


def _module_disabled(module_key: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "module_disabled",
            "module": module_key,
            "message": f"{module_key.replace('_', '-')} module is disabled by backend configuration.",
        },
    )


async def require_optional_module_enabled(module_key: str) -> None:
    if not optional_module_available(module_key):
        raise _module_disabled(module_key)


__all__ = [
    "require_optional_module_enabled",
]
