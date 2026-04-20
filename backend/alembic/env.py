"""Alembic environment. Reads database URL from app settings, uses async engine."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.database import Base

# Import all model modules so Base.metadata sees every table.
# New model files must be imported here for autogenerate to detect them.
from app.models import (  # noqa: F401,E402,A004
    contact,
    discovery,
    outreach,
    property,
    query_bank,
    search_history,
    search_job,
    source,
)

_MODELS_REGISTERED = (
    source,
    query_bank,
    discovery,
    property,
    contact,
    outreach,
    search_history,
    search_job,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
# Escape `%` → `%%` because alembic's config goes through configparser,
# which treats `%(...)s` as interpolation. URL-encoded passwords (e.g.,
# `%40` for `@`) would otherwise blow up with "invalid interpolation syntax".
_db_url = str(settings.database_url).replace("%", "%%")
config.set_main_option("sqlalchemy.url", _db_url)

target_metadata = Base.metadata

# PostGIS installs helper tables (spatial_ref_sys, state, cousub, etc.) into
# the public schema. Autogenerate sees them as "not in models" and would try
# to drop them. This filter skips anything not explicitly in our metadata.
_POSTGIS_TABLES = {
    "spatial_ref_sys",
    "geography_columns",
    "geometry_columns",
    "layer",
    "topology",
    "raster_columns",
    "raster_overviews",
}


def include_object(_obj, name, type_, reflected, _compare_to):  # type: ignore[no-untyped-def]
    if type_ == "table":
        if reflected and name not in target_metadata.tables:
            return False
        if name in _POSTGIS_TABLES:
            return False
    if type_ == "schema" and name in {"tiger", "tiger_data", "topology"}:
        return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in offline mode (SQL script generation only)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in async online mode."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
