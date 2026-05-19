from __future__ import annotations

from alembic.config import Config
from logging.config import fileConfig
from sqlalchemy import inspect, text
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import get_settings
from app.db.models import Base

config = getattr(context, "config", Config())

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
if settings.sqlalchemy_database_url:
    config.set_main_option("sqlalchemy.url", settings.sqlalchemy_database_url)

target_metadata = Base.metadata

CURRENT_BASELINE_REVISION = "0001"
RETIRED_AGENCY_BASELINE_REVISIONS = {"20260517_0002"}


def normalize_retired_baseline_revision(connection: Connection) -> None:
    """Move local DBs stamped with removed agency-only baselines onto the OSS baseline."""

    inspector = inspect(connection)
    if "alembic_version" not in inspector.get_table_names():
        return

    rows = connection.execute(text("SELECT version_num FROM alembic_version")).all()
    current_revisions = {row[0] for row in rows}
    retired_revisions = current_revisions & RETIRED_AGENCY_BASELINE_REVISIONS
    if not retired_revisions:
        return

    supported_revisions = RETIRED_AGENCY_BASELINE_REVISIONS | {CURRENT_BASELINE_REVISION}
    if not current_revisions.issubset(supported_revisions):
        return

    for retired_revision in retired_revisions:
        connection.execute(
            text("DELETE FROM alembic_version WHERE version_num = :revision"),
            {"revision": retired_revision},
        )
    if CURRENT_BASELINE_REVISION not in current_revisions:
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": CURRENT_BASELINE_REVISION},
        )


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
    normalize_retired_baseline_revision(connection)
    if connection.in_transaction():
        connection.commit()

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
