from __future__ import annotations

__all__ = ["app", "create_app"]


def create_app():
    from .api.main import create_app as _create_app

    return _create_app()


def __getattr__(name: str):
    if name == "app":
        return create_app()
    raise AttributeError(name)
