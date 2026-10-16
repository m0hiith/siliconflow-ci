"""scripts/metrics_report.py: every figure comes from the rows in the database."""
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest

from app.models import Artifact, BuildSize, BuildStatus, GateResult, TestCase, TestOutcome
from tests.conftest import BACKEND_DIR, asyncpg_dsn
from tests.factories import make_build, make_project

# The test image mounts the repository's scripts/ here (docker-compose.yml).
SCRIPT = Path(os.environ.get("SFCI_SCRIPTS_DIR", BACKEND_DIR.parent / "scripts")) / "metrics_report.py"

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def report():
    spec = importlib.util.spec_from_file_location("metrics_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _collect(report, database_url):
    conn = await asyncpg.connect(asyncpg_dsn(database_url))
    try:
        return await report.collect(conn)
    finally:
        await conn.close()


async def _build(session, project, status, created_at, seconds=None, **fields):
    b = await make_build(session, project, created_at=created_at, **fields)
    b.status = status
    if seconds is not None:
        b.started_at = created_at
        b.finished_at = created_at + timedelta(seconds=seconds)
    return b


def test_dsn_drops_the_driver(report):
    assert report.asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"


async def test_empty_database_reports_no_builds_not_zero_rates(report, database_url, engine):
    data = await _collect(report, database_url)
    assert data["missing_tables"] == []
    text = report.render(data)
    assert "No builds recorded" in text
    assert "%" not in text


async def test_unmigrated_database_says_so(report, empty_database_url):
    data = await _collect(report, empty_database_url)
    assert "builds" in data["missing_tables"]
    assert "Schema is not at head" in report.render(data)


async def test_figures_match_recorded_rows(report, database_url, session):
    project = await make_project(session)
    p1 = await _build(session, project, BuildStatus.passed, T0, seconds=60)
    await _build(session, project, BuildStatus.passed, T0 + timedelta(hours=1), seconds=120)
    await _build(
        session, project, BuildStatus.failed, T0 + timedelta(hours=2), seconds=30,
        failure_class="link_error",
    )
    await _build(
        session, project, BuildStatus.errored, T0 + timedelta(hours=3), failure_class="infra_ci"
    )
    # Cancelled: counted, but no verdict and no duration.
    await _build(session, project, BuildStatus.cancelled, T0 + timedelta(hours=4), seconds=999)
    m4 = await _build(
        session, project, BuildStatus.passed, T0 + timedelta(hours=5), seconds=90, profile="cortex-m4"
    )
    await session.flush()

    sha = "ab" * 32
    session.add_all(
        [
            Artifact(build_id=p1.id, kind="elf", file_name="firmware", size_bytes=100,
                     sha256=sha, storage_key="sha256/ab/ab/x", release_eligible=True),
            Artifact(build_id=m4.id, kind="elf", file_name="firmware", size_bytes=100, sha256=sha),
            GateResult(build_id=p1.id, rule_id="test_failures", passed=True),
            GateResult(build_id=m4.id, rule_id="test_failures", passed=False),
            TestCase(build_id=p1.id, name="test_a", outcome=TestOutcome.passed),
            TestCase(build_id=m4.id, name="test_a", outcome=TestOutcome.failed),
            BuildSize(build_id=m4.id, file_name="firmware", text_bytes=8448, data_bytes=12, bss_bytes=40),
        ]
    )
    await session.commit()

    data = await _collect(report, database_url)

    assert data["window"][0]["builds"] == 6
    assert {r["status"]: r["n"] for r in data["by_status"]} == {
        "passed": 3, "failed": 1, "errored": 1, "cancelled": 1,
    }
    rates = {r["profile"]: (r["passed"], r["verdicts"]) for r in data["pass_rate"]}
    assert rates == {"host-cmake": (2, 3), "cortex-m4": (1, 1), None: (3, 4)}
    durations = {r["profile"]: r for r in data["durations"]}
    assert durations["host-cmake"]["n"] == 3
    assert durations["host-cmake"]["p50"] == 60
    assert durations["host-cmake"]["max"] == 120
    assert {(r["status"], r["class"]) for r in data["failure_classes"]} == {
        ("failed", "link_error"), ("errored", "infra_ci"),
    }
    a = data["artifacts"][0]
    assert (a["rows"], a["distinct_sha256"], a["stored"], a["release_eligible"]) == (2, 1, 1, 1)
    assert data["gates"] == [{"rule_id": "test_failures", "passed": 1, "failed": 1}]
    assert data["sizes"][0]["text_latest"] == 8448

    text = report.render(data)
    assert "Builds recorded: 6" in text
    assert "75.0% (3/4)" in text
    assert "66.7% (2/3)" in text
