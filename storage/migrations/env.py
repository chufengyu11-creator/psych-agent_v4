"""Alembic environment for offline and asynchronous online migrations."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from storage.models.base import Base
from storage.models.registry import load_all_models

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

load_all_models()
target_metadata = Base.metadata


def get_database_url() -> str:
    """Return the configured database URL without logging it."""

    return get_settings().database_url


config.set_main_option(
    "sqlalchemy.url",
    get_database_url().replace("%", "%%"),
)


def run_migrations_offline() -> None:
    """Generate migration SQL without creating an engine or connection."""

    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure and execute migrations on one synchronous connection."""

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run online migrations through an asynchronous SQLAlchemy engine."""

    configuration = config.get_section(
        config.config_ini_section,
        {},
    )
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Run the asynchronous online migration entry point."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
