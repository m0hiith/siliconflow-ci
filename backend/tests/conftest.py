"""Test fixtures. Tests run against a real Postgres, never SQLite.

SFCI_TEST_ADMIN_DATABASE_URL must point at a database the test user can use to
CREATE/DROP databases (e.g. .../postgres). Each test session creates a fresh
database, migrates it from empty with Alembic, and drops it afterwards.
"""
import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from hypothesis import settings as hypothesis_settings
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.main import create_app
from app.models import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]

# The test image's working directory is read-only; keep Hypothesis's example
# database in memory instead of warning (warnings are errors in this suite).
hypothesis_settings.register_profile("sfci", database=None)
hypothesis_settings.load_profile("sfci")


def admin_url() -> str:
    url = os.environ.get("SFCI_TEST_ADMIN_DATABASE_URL")
    if not url:
        pytest.exit(
            "SFCI_TEST_ADMIN_DATABASE_URL is not set. Run the suite with "
            "`docker compose run --rm --build tests`, or point it at a Postgres "
            "you can create databases in.",
            returncode=2,
        )
    return url


def asyncpg_dsn(sqlalchemy_url: str) -> str:
    return make_url(sqlalchemy_url).set(drivername="postgresql").render_as_string(
        hide_password=False
    )


async def _admin_execute(sql: str) -> None:
    conn = await asyncpg.connect(asyncpg_dsn(admin_url()))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


def create_database() -> str:
    """Create an empty, uniquely named database and return its SQLAlchemy URL."""
    name = f"sfci_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin_execute(f'CREATE DATABASE "{name}"'))
    return make_url(admin_url()).set(database=name).render_as_string(hide_password=False)


def drop_database(url: str) -> None:
    name = make_url(url).database
    asyncio.run(_admin_execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["database_url"] = url
    return cfg


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """A database migrated from empty to head, shared by the session."""
    url = create_database()
    try:
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        drop_database(url)


@pytest.fixture
def empty_database_url() -> Iterator[str]:
    """A brand-new database with no schema, for migration tests."""
    url = create_database()
    try:
        yield url
    finally:
        drop_database(url)


TABLES = [t.name for t in Base.metadata.sorted_tables]


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
        await engine.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(sessionmaker) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s


WEBHOOK_SECRET = "test-webhook-secret"
GITHUB_APP_ID = "123456"
GITHUB_API = "https://api.github.com"


@pytest.fixture(scope="session")
def github_app_key() -> rsa.RSAPrivateKey:
    """A throwaway RSA key standing in for the GitHub App's private key."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def github_app_key_pem(github_app_key: rsa.RSAPrivateKey) -> str:
    return github_app_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def settings(database_url: str, github_app_key_pem: str, tmp_path: Path) -> Settings:
    return Settings(
        database_url=database_url,
        env="test",
        log_level="WARNING",
        github_webhook_secret=WEBHOOK_SECRET,
        github_app_id=GITHUB_APP_ID,
        github_app_private_key=github_app_key_pem,
        github_api_url=GITHUB_API,
        artifact_root=str(tmp_path / "artifacts"),
    )


@pytest.fixture
async def app(settings: Settings, engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    # Depends on `engine` so tables are truncated after each API test too.
    app = create_app(settings)
    try:
        yield app
    finally:
        await app.state.github.aclose()
        await app.state.oidc.aclose()
        await app.state.engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
