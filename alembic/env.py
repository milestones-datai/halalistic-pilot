"""Alembic env.py — async-aware.

Reads DATABASE_URL from app settings, points target_metadata at
`app.db.base.Base.metadata`. Stage 2 imports the user model; future stages
should add their own model imports here.
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.db.base import Base

# Stage 2: user + auth tables.
# Stage 3: restaurant, cuisine, photo, menu tables.
# Stage 4: halal certificates + certifying body lookup.
from app.models import user  # noqa: F401
from app.models import restaurant  # noqa: F401
from app.models import menu  # noqa: F401
from app.models import certifying_body  # noqa: F401
from app.models import halal_certificate  # noqa: F401

config = context.config

config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async Engine and associate a connection with the context."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode using an async engine."""
    asyncio.run(run_async_migrations())


run_migrations_online()
