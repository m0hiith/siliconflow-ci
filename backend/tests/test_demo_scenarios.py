"""Demo scenarios, replayed through the real API, worker and services against
a real Postgres database named sfci_demo (the only one demo mode accepts).

The replay's pauses run the worker until it is idle, so each step sees what
the worker made of the previous one, as it would at demo speed.
"""
import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, make_url, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import DEMO_DATABASE, Settings
from app.demo.replay import DemoReplay
from app.demo.source import DemoRepoSource
from app.main import create_app
from app.models import Build, Repository
from tests.conftest import BACKEND_DIR, TABLES, _admin_execute, admin_url, alembic_config
from tests.github import drain

DEMO_DIR = Path(os.environ.get("SFCI_DEMO_DIR", BACKEND_DIR.parent / "demo"))


@pytest.fixture(scope="module")
def demo_database_url() -> Iterator[str]:
    """A fresh sfci_demo database. Never one that already exists: it could
    be someone's demo data."""
    import asyncpg

    from tests.conftest import asyncpg_dsn

    async def exists() -> bool:
        conn = await asyncpg.connect(asyncpg_dsn(admin_url()))
        try:
            return bool(await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", DEMO_DATABASE))
        finally:
            await conn.close()

    if asyncio.run(exists()):
        pytest.fail(f"database {DEMO_DATABASE!r} already exists on the test server; drop it to run these tests")
    asyncio.run(_admin_execute(f'CREATE DATABASE "{DEMO_DATABASE}"'))
    url = make_url(admin_url()).set(database=DEMO_DATABASE).render_as_string(hide_password=False)
    try:
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        asyncio.run(_admin_execute(f'DROP DATABASE IF EXISTS "{DEMO_DATABASE}" WITH (FORCE)'))


@pytest.fixture
def demo_settings(demo_database_url: str, tmp_path: Path) -> Settings:
    return Settings(
        database_url=demo_database_url,
        env="test",
        log_level="WARNING",
        demo_mode=True,
        demo_dir=str(DEMO_DIR),
        artifact_root=str(tmp_path / "artifacts"),
    )


@pytest.fixture
async def demo_app(demo_settings: Settings) -> AsyncIterator[FastAPI]:
    app = create_app(demo_settings)
    try:
        yield app
    finally:
        await app.state.demo_replay.aclose()
        await app.state.github.aclose()
        await app.state.oidc.aclose()
        await app.state.engine.dispose()
        engine = create_async_engine(demo_settings.database_url.get_secret_value(), poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
        await engine.dispose()


@pytest.fixture
async def demo_client(demo_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=demo_app), base_url="http://test") as c:
        yield c


@pytest.fixture
def worker(demo_app: FastAPI, demo_settings: Settings):
    """Runs the worker, with the demo source, until no job is ready."""
    source = DemoRepoSource(demo_settings, demo_app.state.demo_replay.fixtures)

    async def run(_seconds: float = 0) -> None:
        await drain(demo_app.state.sessionmaker, demo_settings, source)

    return run


@pytest.fixture
def replay(demo_app: FastAPI, worker):
    async def run(name: str):
        demo: DemoReplay = demo_app.state.demo_replay
        result = await demo.replay(demo.fixtures.scenarios[name], worker)
        await worker()
        assert result.state == "done", result.error
        return result

    return run


async def get(client: AsyncClient, path: str):
    r = await client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


def gates_by_rule(gates: list[dict]) -> dict[str, dict]:
    return {g["rule_id"]: g for g in gates}


# ------------------------------------------------------------------- tests


async def test_meta_says_demo(demo_client):
    assert (await get(demo_client, "/api/v1/meta"))["demo_mode"] is True


async def test_scenarios_are_listed_idle(demo_client):
    scenarios = await get(demo_client, "/api/v1/demo/scenarios")
    assert {s["name"] for s in scenarios} == {
        "clean_main", "compile_error", "passed_not_releasable", "redelivery", "infra_hw", "tamper", "history",
    }
    assert {s["state"] for s in scenarios} == {"idle"}


async def test_unknown_scenario_is_404(demo_client):
    r = await demo_client.post("/api/v1/demo/scenarios/nope")
    assert r.status_code == 404


async def test_github_tokens_are_not_trusted_in_demo_mode(demo_client):
    from tests import oidc, reports

    token = reports.token(oidc.new_key())
    r = await reports.post(demo_client, reports.report(), token)
    assert (r.status_code, r.json()) == (401, {"detail": "invalid token"})


async def test_clean_main_is_release_eligible(demo_client, replay):
    run = await replay("clean_main")
    build_id = run.build_ids["clean"]
    build = await get(demo_client, f"/api/v1/builds/{build_id}")
    assert build["status"] == "passed"
    assert build["artifacts_state"] == "stored"
    gates = await get(demo_client, f"/api/v1/builds/{build_id}/gates")
    assert gates and all(g["passed"] for g in gates), gates
    artifacts = await get(demo_client, f"/api/v1/builds/{build_id}/artifacts")
    assert {a["file_name"] for a in artifacts} == {"firmware", "firmware.bin"}
    assert all(a["release_eligible"] for a in artifacts)
    tests = await get(demo_client, f"/api/v1/builds/{build_id}/tests")
    assert tests and all(t["outcome"] == "passed" for t in tests)


async def test_compile_error_is_classified_with_its_first_error(demo_client, replay):
    run = await replay("compile_error")
    build_id = run.build_ids["broken"]
    build = await get(demo_client, f"/api/v1/builds/{build_id}")
    assert (build["status"], build["failure_class"]) == ("failed", "compile_error")
    log = await get(demo_client, f"/api/v1/builds/{build_id}/logs/compile")
    assert log["failure_class"] == "compile_error"
    first = log["first_error"]
    assert first["file"].endswith("src/sensor_logic.c")
    assert first["line"] == 74
    assert "undeclared" in first["message"]
    assert await get(demo_client, f"/api/v1/builds/{build_id}/gates") == []


async def test_passed_but_text_size_gate_fails(demo_client, replay):
    run = await replay("passed_not_releasable")
    build_id = run.build_ids["too_big"]
    assert (await get(demo_client, f"/api/v1/builds/{build_id}"))["status"] == "passed"
    gates = gates_by_rule(await get(demo_client, f"/api/v1/builds/{build_id}/gates"))
    size = gates["text_bytes_max"]
    # Measured by the server from the stored ELF; the report states no size.
    assert (size["passed"], size["observed"], size["threshold"]) == (False, 30063, 20480)
    assert [rule for rule, g in gates.items() if not g["passed"]] == ["text_bytes_max"]
    artifacts = await get(demo_client, f"/api/v1/builds/{build_id}/artifacts")
    assert artifacts and not any(a["release_eligible"] for a in artifacts)


async def test_redelivery_is_deduplicated(demo_client, demo_app, replay):
    run = await replay("redelivery")
    completed = [e for e in run.events if e["step"] == "workflow_run.completed"]
    assert [e["result"] for e in completed] == ["accepted", "duplicate", "duplicate"]
    assert len({e["delivery_id"] for e in completed}) == 1
    build = await get(demo_client, f"/api/v1/builds/{run.build_ids['redelivered']}")
    assert build["status"] == "passed"
    async with demo_app.state.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Build)) == 1


async def test_replaying_a_scenario_again_changes_nothing(demo_client, demo_app, replay):
    first = await replay("redelivery")
    build_id = first.build_ids["redelivered"]
    before = await get(demo_client, f"/api/v1/builds/{build_id}")
    again = await replay("redelivery")
    assert again.build_ids == first.build_ids
    report = [e for e in again.events if e["step"] == "run_report"]
    assert [e["result"] for e in report] == ["duplicate"]
    assert await get(demo_client, f"/api/v1/builds/{build_id}") == before
    async with demo_app.state.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Build)) == 1


async def test_hil_without_jlink_is_infra_hw(demo_client, replay):
    run = await replay("infra_hw")
    build_id = run.build_ids["soc"]
    build = await get(demo_client, f"/api/v1/builds/{build_id}")
    # Hardware results never change a finished build's status.
    assert (build["status"], build["profile"]) == ("passed", "siwx917-soc")
    stages = {s["name"]: s for s in await get(demo_client, f"/api/v1/builds/{build_id}/stages")}
    hil = stages["hil"]
    assert (hil["status"], hil["exit_code"], hil["failure_class"]) == ("failed", 75, "infra_hw")
    assert [e["result"] for e in run.events if e["step"] == "linked_report"] == ["accepted"]


async def test_tampered_artifact_is_rejected_and_nothing_stored(demo_client, demo_settings, replay):
    run = await replay("tamper")
    build_id = run.build_ids["tampered"]
    build = await get(demo_client, f"/api/v1/builds/{build_id}")
    assert (build["status"], build["artifacts_state"]) == ("passed", "rejected")
    assert await get(demo_client, f"/api/v1/builds/{build_id}/artifacts") == []
    assert await get(demo_client, f"/api/v1/builds/{build_id}/tests") == []
    gates = gates_by_rule(await get(demo_client, f"/api/v1/builds/{build_id}/gates"))
    assert gates["artifacts_verified"]["passed"] is False
    stored = Path(demo_settings.artifact_root, "sha256")
    assert not stored.exists() or not any(p.is_file() for p in stored.rglob("*"))


async def test_history_fills_analytics(demo_client, demo_app, replay):
    await replay("history")
    async with demo_app.state.sessionmaker() as session:
        statuses = (
            await session.execute(select(Build.status, func.count()).group_by(Build.status))
        ).all()
        repos = await session.scalar(select(func.count()).select_from(Repository))
    counts = {status.value: n for status, n in statuses}
    assert sum(counts.values()) == 25
    assert counts == {"passed": 22, "failed": 3}
    assert repos == 1

    rate = await get(demo_client, "/api/v1/analytics/pass-rate?weeks=8")
    assert rate["enough_data"] is True
    sizes = await get(demo_client, "/api/v1/analytics/sizes")
    m4 = next(s for s in sizes["series"] if s["profile"] == "cortex-m4")
    assert m4["enough_data"] is True
    text_bytes = [c["text_bytes"] for c in m4["commits"]]
    assert text_bytes == sorted(text_bytes) and text_bytes[0] < text_bytes[-1]
    flaky = await get(demo_client, "/api/v1/analytics/flaky-tests")
    assert [(t["profile"], len(t["same_commit_shas"])) for t in flaky["tests"]] == [("host-cmake", 1)]


async def test_start_endpoint_replays_in_the_background(demo_client, demo_app, worker):
    r = await demo_client.post("/api/v1/demo/scenarios/clean_main?speed=100")
    assert r.status_code == 202
    assert r.json()["state"] == "running"
    again = await demo_client.post("/api/v1/demo/scenarios/clean_main")
    assert again.status_code == 409
    for _ in range(200):
        await worker()
        listed = {s["name"]: s for s in await get(demo_client, "/api/v1/demo/scenarios")}
        if listed["clean_main"]["state"] != "running":
            break
        await asyncio.sleep(0.05)
    assert listed["clean_main"]["state"] == "done", listed["clean_main"]
    assert set(listed["clean_main"]["build_ids"]) == {"clean"}
