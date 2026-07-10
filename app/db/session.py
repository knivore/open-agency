"""Async SQLAlchemy engine/session lifecycle helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from typing import Any

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_engine_url: str | None = None


def _build_engine_kwargs(database_url: str, *, echo: bool, pool_size: int, max_overflow: int) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if not database_url.startswith("sqlite+aiosqlite://"):
        kwargs["pool_size"] = pool_size
        kwargs["max_overflow"] = max_overflow
    return kwargs


def is_database_configured() -> bool:
    return get_settings().database_enabled


def get_async_engine(*, optional: bool = False) -> AsyncEngine | None:
    global _engine, _engine_url

    settings = get_settings()
    database_url = settings.sqlalchemy_database_url
    if not database_url:
        if optional:
            return None
        raise RuntimeError("DATABASE_URL is not configured")

    if _engine is not None and _engine_url == database_url:
        return _engine

    _engine = create_async_engine(
        database_url,
        **_build_engine_kwargs(
            database_url,
            echo=settings.database_echo,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
        ),
    )
    _engine_url = database_url
    return _engine


def get_session_maker(*, optional: bool = False) -> async_sessionmaker[AsyncSession] | None:
    global _sessionmaker

    engine = get_async_engine(optional=optional)
    if engine is None:
        return None
    if _sessionmaker is None or _engine is not engine:
        _sessionmaker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    return _sessionmaker


async def get_async_session() -> AsyncIterator[AsyncSession]:
    session_factory = get_session_maker()
    if session_factory is None:
        raise RuntimeError("DATABASE_URL is not configured")
    async with session_factory() as session:
        yield session


async def ping_database() -> bool:
    engine = get_async_engine(optional=True)
    if engine is None:
        return False
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return True


def reset_session_state() -> None:
    global _engine, _sessionmaker, _engine_url
    _engine = None
    _sessionmaker = None
    _engine_url = None
