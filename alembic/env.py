from __future__ import annotations

from alembic.config import Config
from logging.config import fileConfig
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import get_settings
from app.db.models import Base
from app.modules.registry import load_optional_module_orm_models, optional_module_alembic_version_locations

config = getattr(context, "config", Config())

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
if settings.sqlalchemy_database_url:
    config.set_main_option("sqlalchemy.url", settings.sqlalchemy_database_url)

load_optional_module_orm_models()
config.set_main_option("version_locations", " ".join(optional_module_alembic_version_locations()))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


try:
    offline_mode = context.is_offline_mode()
except (AttributeError, NameError):
    offline_mode = None

if offline_mode is not None:
    if offline_mode:
        run_migrations_offline()
    else:
        import asyncio

        asyncio.run(run_migrations_online())
