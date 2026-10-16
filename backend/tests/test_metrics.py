from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1 import metrics
from app.models import BuildStatus
from tests.factories import make_build, make_project

NOW = datetime(2026, 10, 14, 15, 30, tzinfo=timezone.utc)
MIDNIGHT = datetime(2026, 10, 14, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    monkeypatch.setattr(metrics, "utcnow", lambda: NOW)


async def _build(session, project, status, created_at, minutes=None, **fields):
    """A build in `status`; `minutes` sets a started-to-finished duration."""
    b = await make_build(session, project, created_at=created_at, **fields)
    b.status = status
    if minutes is not None:
        b.started_at = created_at
        b.finished_at = created_at + timedelta(minutes=minutes)
    return b


async def test_summary_with_no_builds_is_empty_not_zero_rate(client):
    r = await client.get("/api/v1/metrics/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["builds_today"] == 0
    assert body["failed_today"] == 0
    assert body["active_builds"] == 0
    assert body["pass_rate"] is None
    assert body["median_duration_seconds"] is None
    assert body["recent_failures"] == []
    assert body["status_counts"] == {s.value: 0 for s in BuildStatus}
    assert datetime.fromisoformat(body["window_start"]) == MIDNIGHT


async def test_summary_counts_today_only(client, session):
    p = await make_project(session, "fw")
    t = MIDNIGHT + timedelta(hours=1)
    await _build(session, p, BuildStatus.passed, t, minutes=2)
    await _build(session, p, BuildStatus.passed, t + timedelta(minutes=5), minutes=4)
    await _build(session, p, BuildStatus.failed, t + timedelta(minutes=10), minutes=10)
    await _build(session, p, BuildStatus.errored, t + timedelta(minutes=15), minutes=60)
    await _build(session, p, BuildStatus.cancelled, t + timedelta(minutes=20), minutes=90)
    await _build(session, p, BuildStatus.running, t + timedelta(minutes=25))
    # Yesterday: excluded from today's numbers, though it is still in recent failures.
    await _build(session, p, BuildStatus.failed, MIDNIGHT - timedelta(seconds=1), minutes=1)
    await session.commit()

    body = (await client.get("/api/v1/metrics/summary")).json()
    assert body["builds_today"] == 6
    assert body["status_counts"]["passed"] == 2
    assert body["status_counts"]["errored"] == 1
    assert body["status_counts"]["queued"] == 0
    assert body["failed_today"] == 1
    # errored and cancelled builds carry no firmware verdict.
    assert body["pass_rate"] == pytest.approx(2 / 3)
    assert body["median_duration_seconds"] == pytest.approx(240.0)
    assert body["active_builds"] == 1
    assert len(body["recent_failures"]) == 2


async def test_active_builds_include_older_days(client, session):
    p = await make_project(session)
    await _build(session, p, BuildStatus.queued, MIDNIGHT - timedelta(days=2))
    await _build(session, p, BuildStatus.running, MIDNIGHT + timedelta(hours=1))
    await session.commit()

    body = (await client.get("/api/v1/metrics/summary")).json()
    assert body["active_builds"] == 2
    assert body["builds_today"] == 1
    assert body["pass_rate"] is None


async def test_median_skips_builds_without_timestamps(client, session):
    p = await make_project(session)
    t = MIDNIGHT + timedelta(hours=2)
    await _build(session, p, BuildStatus.failed, t)  # never started
    await _build(session, p, BuildStatus.passed, t, minutes=3)
    await session.commit()

    body = (await client.get("/api/v1/metrics/summary")).json()
    assert body["median_duration_seconds"] == pytest.approx(180.0)
    assert body["pass_rate"] == pytest.approx(0.5)


async def test_recent_failures_newest_finished_first_and_capped(client, session):
    p = await make_project(session)
    t = MIDNIGHT - timedelta(days=3)
    builds = [
        await _build(session, p, BuildStatus.failed, t + timedelta(hours=i), minutes=1)
        for i in range(metrics.RECENT_FAILURES + 2)
    ]
    await _build(session, p, BuildStatus.timed_out, t, minutes=1)
    await session.commit()

    body = (await client.get("/api/v1/metrics/summary")).json()
    ids = [b["id"] for b in body["recent_failures"]]
    assert ids == [b.id for b in reversed(builds)][: metrics.RECENT_FAILURES]
    assert all(b["status"] == "failed" for b in body["recent_failures"])


async def test_summary_filters_by_project(client, session):
    fw = await make_project(session, "fw")
    other = await make_project(session, "other")
    t = MIDNIGHT + timedelta(hours=1)
    await _build(session, fw, BuildStatus.passed, t, minutes=1)
    await _build(session, other, BuildStatus.failed, t, minutes=1)
    await _build(session, other, BuildStatus.running, t)
    await session.commit()

    body = (await client.get("/api/v1/metrics/summary", params={"project_id": fw.id})).json()
    assert body["builds_today"] == 1
    assert body["pass_rate"] == 1.0
    assert body["active_builds"] == 0
    assert body["recent_failures"] == []

    body = (await client.get("/api/v1/metrics/summary")).json()
    assert body["builds_today"] == 3
    assert body["failed_today"] == 1
