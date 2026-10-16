"""Alembic environment (async, asyncpg).

The database URL comes from, in order:
  1. config.attributes["database_url"]  (set programmatically, e.g. by tests)
  2. the SFCI_DATABASE_URL environment variable (via app.config.Settings)
"""
import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.models import Base

config = context.config
target_metadata = Base.metadata

# From the CLI (`alembic upgrade head`), log migration steps as JSON. When run
# programmatically (tests), leave the caller's logging alone.
if "database_url" not in config.attributes:
    from app.logs import configure_logging

    configure_logging("INFO")


def database_url() -> str:
    url = config.attributes.get("database_url")
    if url:
        return url
    from app.config import Settings

    return Settings().database_url.get_secret_value()  # type: ignore[call-arg]


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
