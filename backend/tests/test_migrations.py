import asyncio

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.models import Base
from tests.conftest import alembic_config

EXPECTED_TABLES = {
    "users",
    "projects",
    "repositories",
    "builds",
    "build_stages",
    "test_cases",
    "artifacts",
    "gate_results",
    "webhook_events",
    "audit_log",
    "job_queue",
    "run_reports",
    "build_sizes",
}

EXPECTED_ENUMS = {
    "build_status": ["queued", "running", "passed", "failed", "cancelled", "timed_out", "errored"],
    "stage_status": ["pending", "running", "passed", "failed", "skipped", "cancelled"],
}


async def _tables_and_enums(url: str) -> tuple[set[str], dict[str, list[str]]]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tables = set(
                (
                    await conn.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    )
                ).scalars()
            )
            rows = await conn.execute(
                text(
                    "SELECT t.typname, e.enumlabel FROM pg_type t "
                    "JOIN pg_enum e ON e.enumtypid = t.oid ORDER BY t.typname, e.enumsortorder"
                )
            )
            enums: dict[str, list[str]] = {}
            for typname, label in rows:
                enums.setdefault(typname, []).append(label)
        return tables, enums
    finally:
        await engine.dispose()


async def test_upgrade_from_empty_downgrade_and_upgrade_again(empty_database_url):
    cfg = alembic_config(empty_database_url)

    # Alembic's env.py runs its own event loop, so drive it from a thread.
    await asyncio.to_thread(command.upgrade, cfg, "head")
    tables, enums = await _tables_and_enums(empty_database_url)
    assert EXPECTED_TABLES <= tables
    for name, values in EXPECTED_ENUMS.items():
        assert enums[name] == values

    await asyncio.to_thread(command.downgrade, cfg, "base")
    tables, enums = await _tables_and_enums(empty_database_url)
    assert tables == {"alembic_version"}
    assert enums == {}

    await asyncio.to_thread(command.upgrade, cfg, "head")
    tables, _ = await _tables_and_enums(empty_database_url)
    assert EXPECTED_TABLES <= tables


async def test_models_match_migrated_schema(database_url):
    """The ORM models and the migrations must describe the same schema."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            diff = await conn.run_sync(
                lambda sync_conn: compare_metadata(
                    MigrationContext.configure(sync_conn, opts={"compare_type": True}),
                    Base.metadata,
                )
            )
    finally:
        await engine.dispose()
    assert diff == []
