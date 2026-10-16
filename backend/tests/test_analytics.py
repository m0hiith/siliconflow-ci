from datetime import date, datetime, timedelta, timezone

import pytest

from app.api.v1 import analytics
from app.models import BuildSize, BuildStatus, BuildTrigger, TestCase, TestOutcome
from app.services.analytics import MIN_BUILDS, flip_indices, week_start
from tests.factories import make_build, make_project

# A Wednesday; its ISO week starts Monday 2026-10-12.
NOW = datetime(2026, 10, 14, 15, 30, tzinfo=timezone.utc)
MIDNIGHT = datetime(2026, 10, 14, tzinfo=timezone.utc)
MONDAY = datetime(2026, 10, 12, tzinfo=timezone.utc)

P, F, S, E = TestOutcome.passed, TestOutcome.failed, TestOutcome.skipped, TestOutcome.error


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    monkeypatch.setattr(analytics, "utcnow", lambda: NOW)


def sha(n: int) -> str:
    return f"{n:040x}"


async def _build(session, project, status, created_at, minutes=None, **fields):
    """A build in `status`; `minutes` sets a started-to-finished duration."""
    b = await make_build(session, project, created_at=created_at, **fields)
    b.status = status
    if minutes is not None:
        b.started_at = created_at
        b.finished_at = created_at + timedelta(minutes=minutes)
    return b


async def _filler(session, project, n, created_at, **fields):
    """n passed one-minute builds, to get past MIN_BUILDS."""
    for i in range(n):
        await _build(session, project, BuildStatus.passed, created_at + timedelta(seconds=i), 1, **fields)


# ------------------------------------------------------------------ helpers


def test_flip_indices():
    assert flip_indices([]) == []
    assert flip_indices([P, P, P]) == []
    assert flip_indices([P, F, P, F]) == [1, 2, 3]
    # error is a failure; skipped neither counts nor breaks the sequence.
    assert flip_indices([P, E, F, P]) == [1, 3]
    assert flip_indices([P, S, F, S, S, F]) == [2]
    assert flip_indices([S, S]) == []


def test_week_start_is_monday_midnight():
    assert week_start(NOW) == MONDAY
    assert week_start(MONDAY) == MONDAY
    assert week_start(MONDAY - timedelta(microseconds=1)) == MONDAY - timedelta(days=7)


# --------------------------------------------------------------- durations


async def test_durations_not_enough_data(client, session):
    p = await make_project(session)
    await _filler(session, p, MIN_BUILDS - 1, MIDNIGHT)
    await session.commit()

    body = (await client.get("/api/v1/analytics/durations")).json()
    assert body["builds"] == MIN_BUILDS - 1
    assert body["min_builds"] == MIN_BUILDS
    assert body["enough_data"] is False
    assert body["days"] == []


async def test_durations_median_and_p90_per_profile_and_day(client, session):
    p = await make_project(session)
    day1 = MIDNIGHT - timedelta(days=3, hours=-1)
    for minutes in (1, 2, 3, 4, 10):
        await _build(session, p, BuildStatus.passed, day1, minutes)
    await _build(session, p, BuildStatus.failed, MIDNIGHT + timedelta(hours=1), 6)
    for minutes in (2, 4, 6, 8):
        await _build(session, p, BuildStatus.passed, MIDNIGHT, minutes, profile="cortex-m4")
    # No firmware verdict, never started, or outside the window: ignored.
    await _build(session, p, BuildStatus.errored, MIDNIGHT, 90)
    await _build(session, p, BuildStatus.timed_out, MIDNIGHT, 90)
    await _build(session, p, BuildStatus.failed, MIDNIGHT)
    await _build(session, p, BuildStatus.passed, MIDNIGHT - timedelta(days=30), 1)
    await session.commit()

    body = (await client.get("/api/v1/analytics/durations")).json()
    assert body["builds"] == 10
    assert body["enough_data"] is True
    assert datetime.fromisoformat(body["window_start"]) == MIDNIGHT - timedelta(days=29)
    # Only the days that had builds; the two days in between have no point.
    assert [(d["day"], d["profile"], d["builds"]) for d in body["days"]] == [
        ("2026-10-11", "host-cmake", 5),
        ("2026-10-14", "cortex-m4", 4),
        ("2026-10-14", "host-cmake", 1),
    ]
    d1, m4, today = body["days"]
    assert d1["median_seconds"] == pytest.approx(180.0)
    # percentile_cont: 4 + 0.6 * (10 - 4) minutes.
    assert d1["p90_seconds"] == pytest.approx(7.6 * 60)
    assert m4["median_seconds"] == pytest.approx(300.0)
    assert today["median_seconds"] == today["p90_seconds"] == pytest.approx(360.0)


async def test_durations_day_is_utc_and_scoped_to_project(client, session):
    p = await make_project(session)
    other = await make_project(session)
    await _filler(session, p, MIN_BUILDS, MIDNIGHT - timedelta(seconds=MIN_BUILDS))
    await _filler(session, other, 3, MIDNIGHT)
    await session.commit()

    body = (
        await client.get("/api/v1/analytics/durations", params={"project_id": p.id})
    ).json()
    assert body["builds"] == MIN_BUILDS
    assert [d["day"] for d in body["days"]] == ["2026-10-13"]


# --------------------------------------------------------------- pass rate


async def test_pass_rate_per_week_with_failure_classes(client, session):
    p = await make_project(session)
    last_week = MONDAY - timedelta(seconds=1)  # Sunday 23:59:59
    await _build(session, p, BuildStatus.passed, last_week)
    await _build(session, p, BuildStatus.failed, last_week, failure_class="compile_error")
    for _ in range(3):
        await _build(session, p, BuildStatus.passed, MONDAY)
    await _build(session, p, BuildStatus.failed, MONDAY, failure_class="test_failure")
    await _build(session, p, BuildStatus.failed, MONDAY, failure_class="test_failure")
    await _build(session, p, BuildStatus.failed, MONDAY)  # unclassified
    await _build(session, p, BuildStatus.errored, MONDAY, failure_class="infra_ci")
    await _build(session, p, BuildStatus.cancelled, MONDAY)
    await _build(session, p, BuildStatus.running, MONDAY)  # not terminal
    await session.commit()

    body = (await client.get("/api/v1/analytics/pass-rate")).json()
    assert body["builds"] == 10
    assert body["enough_data"] is True
    assert datetime.fromisoformat(body["window_start"]) == MONDAY - timedelta(weeks=11)
    prev, this = body["weeks"]
    assert prev["week_start"] == "2026-10-05"
    assert prev["builds"] == 2
    assert prev["pass_rate"] == 0.5
    assert prev["failure_classes"] == [{"failure_class": "compile_error", "count": 1}]

    assert this["week_start"] == "2026-10-12"
    assert this["builds"] == 8
    assert this["status_counts"] == {
        "passed": 3, "failed": 3, "cancelled": 1, "timed_out": 0, "errored": 1,
    }
    # errored and cancelled carry no firmware verdict.
    assert this["pass_rate"] == 0.5
    assert this["failure_classes"] == [
        {"failure_class": "test_failure", "count": 2},
        {"failure_class": "infra_ci", "count": 1},
        {"failure_class": None, "count": 1},
    ]


async def test_pass_rate_not_enough_data_and_null_rate(client, session):
    p = await make_project(session)
    for _ in range(MIN_BUILDS - 1):
        await _build(session, p, BuildStatus.passed, MONDAY)
    await session.commit()
    body = (await client.get("/api/v1/analytics/pass-rate")).json()
    assert body["enough_data"] is False and body["weeks"] == []

    await _build(session, p, BuildStatus.errored, MONDAY - timedelta(weeks=1))
    await session.commit()
    body = (await client.get("/api/v1/analytics/pass-rate")).json()
    assert body["enough_data"] is True
    assert body["weeks"][0]["pass_rate"] is None


# ------------------------------------------------------------------- sizes


async def _sized(session, project, profile, n, text, data=100, bss=1000, **fields):
    fields.setdefault("commit_sha", sha(n))
    b = await _build(
        session, project, fields.pop("status", BuildStatus.passed),
        MONDAY + timedelta(minutes=n), profile=profile, **fields,
    )
    session.add(
        BuildSize(build_id=b.id, file_name="firmware.elf", text_bytes=text, data_bytes=data, bss_bytes=bss)
    )
    await session.flush()
    return b


async def test_sizes_per_commit_on_default_branch(client, session):
    p = await make_project(session)
    for n in range(1, MIN_BUILDS):
        await _sized(session, p, "cortex-m4", n, text=7000 + n)
    # Commit 9 built again later (a re-run): its latest build is the point,
    # even though that one failed (a test failure does not change the image).
    rerun = await _sized(
        session, p, "cortex-m4", 20, text=9999, commit_sha=sha(9), status=BuildStatus.failed
    )
    # Not on the default branch, or no commit: never a point.
    await _sized(session, p, "cortex-m4", 30, text=1, branch="feature")
    await _sized(
        session, p, "cortex-m4", 31, text=1, trigger=BuildTrigger.pull_request,
        branch="feature", commit_sha=sha(31),
    )
    await _sized(session, p, "cortex-m4", 32, text=1, commit_sha=None)
    await _sized(session, p, "siwx917-soc", 1, text=50000)
    await session.commit()

    body = (await client.get("/api/v1/analytics/sizes")).json()
    assert body["min_builds"] == MIN_BUILDS
    m4, soc = body["series"]
    assert m4["profile"] == "cortex-m4"
    assert m4["builds"] == 10
    assert m4["enough_data"] is True
    assert [c["commit_sha"] for c in m4["commits"]] == [sha(n) for n in range(1, 10)]
    last = m4["commits"][-1]
    assert last["build_id"] == rerun.id
    assert last["status"] == "failed"
    assert last["flash_bytes"] == 9999 + 100
    assert last["ram_bytes"] == 100 + 1000
    assert m4["commits"][0]["flash_bytes"] == 7001 + 100

    assert soc == {"profile": "siwx917-soc", "builds": 1, "enough_data": False, "commits": []}


async def test_sizes_profile_param_and_limit(client, session):
    p = await make_project(session)
    for n in range(1, 13):
        await _sized(session, p, "host-cmake", n, text=n)
    await session.commit()

    body = (
        await client.get("/api/v1/analytics/sizes", params={"profile": "host-cmake", "limit": 3})
    ).json()
    (series,) = body["series"]
    assert [c["text_bytes"] for c in series["commits"]] == [10, 11, 12]


# ------------------------------------------------------------- flaky tests


async def _ran(session, build, outcomes: dict[str, TestOutcome]):
    for name, outcome in outcomes.items():
        session.add(TestCase(build_id=build.id, suite="unit", name=name, outcome=outcome))
    await session.flush()


async def test_flaky_not_enough_data(client, session):
    p = await make_project(session)
    for i in range(MIN_BUILDS - 1):
        b = await _build(session, p, BuildStatus.passed, MIDNIGHT, commit_sha=sha(1))
        await _ran(session, b, {"t": P if i else F})
    await _build(session, p, BuildStatus.passed, MIDNIGHT)  # no tests: not counted
    await session.commit()

    body = (await client.get("/api/v1/analytics/flaky-tests")).json()
    assert body["builds"] == MIN_BUILDS - 1
    assert body["enough_data"] is False
    assert body["tests"] == []


async def test_flaky_same_commit(client, session):
    p = await make_project(session)
    t = MIDNIGHT - timedelta(days=1)
    builds = []
    for i, outcome in enumerate([P, E, F, P]):
        b = await _build(session, p, BuildStatus.passed, t + timedelta(minutes=i), commit_sha=sha(7))
        await _ran(session, b, {"flaky": outcome, "stable": P, "skippy": S if i else F})
        builds.append(b)
    # Same test, another commit, failing every time: broken, not flaky.
    for i in range(6):
        b = await _build(session, p, BuildStatus.failed, t + timedelta(hours=1, minutes=i), commit_sha=sha(8))
        await _ran(session, b, {"broken": F})
    # Passed and failed on one commit, but before the window.
    for outcome in (P, F):
        b = await _build(session, p, BuildStatus.passed, MIDNIGHT - timedelta(days=60), commit_sha=sha(9))
        await _ran(session, b, {"old": outcome})
    await session.commit()

    body = (await client.get("/api/v1/analytics/flaky-tests")).json()
    assert body["enough_data"] is True
    assert body["recent_builds"] == 20 and body["flip_threshold"] == 3
    (test,) = [t for t in body["tests"] if t["name"] == "flaky"]
    assert [t["name"] for t in body["tests"]] == ["flaky"]
    assert test["repository"] == f"acme/{p.slug}"
    assert test["profile"] == "host-cmake"
    assert test["same_commit_shas"] == [sha(7)]
    # Default-branch history P, E, F, P has 2 flips: not above the threshold.
    assert test["flips"] is None
    # The latest pass and the latest failure on that commit.
    assert [(e["build_id"], e["outcome"]) for e in test["examples"]] == [
        (builds[3].id, "passed"),
        (builds[2].id, "failed"),
    ]


async def test_flaky_flips_over_recent_default_branch_builds(client, session):
    p = await make_project(session)
    old = MIDNIGHT - timedelta(days=200)  # outside the same-commit window on purpose
    # 5 flips in the last 20 builds of host-cmake on main (the skip is ignored).
    seq = [P, F, P, P, F, S, P, F] + [F] * 12
    builds = []
    for i, outcome in enumerate(seq):
        b = await _build(session, p, BuildStatus.passed, old + timedelta(minutes=i), commit_sha=sha(i + 1))
        await _ran(session, b, {"wobbly": outcome, "three_flips": [P, F, P, F][i] if i < 4 else F})
        builds.append(b)
    # Older than the last 20: the flip it would add does not count.
    b = await _build(session, p, BuildStatus.passed, old - timedelta(days=1), commit_sha=sha(99))
    await _ran(session, b, {"three_flips": F})
    # Not on main, or not finished: not part of the recent 20.
    for i, outcome in enumerate([P, F, P, F, P]):
        b = await _build(session, p, BuildStatus.passed, old + timedelta(hours=1, minutes=i),
                         branch="feature", commit_sha=sha(200 + i))
        await _ran(session, b, {"three_flips": outcome})
    b = await _build(session, p, BuildStatus.running, old + timedelta(hours=2), commit_sha=sha(300))
    await _ran(session, b, {"three_flips": P})
    await session.commit()

    body = (await client.get("/api/v1/analytics/flaky-tests")).json()
    assert [t["name"] for t in body["tests"]] == ["wobbly"]
    (test,) = body["tests"]
    assert test["flips"] == 5
    assert test["same_commit_shas"] == []
    # The builds where it flipped, newest first.
    assert [e["build_number"] for e in test["examples"]] == [
        builds[i].number for i in (7, 6, 4, 2)
    ]
    assert [e["outcome"] for e in test["examples"]] == ["failed", "passed", "failed", "passed"]


async def test_flaky_tests_are_per_profile(client, session):
    p = await make_project(session)
    t = MIDNIGHT - timedelta(days=1)
    # The same test passes on one profile and fails on another for one commit.
    for i in range(5):
        for profile, outcome in (("host-cmake", P), ("siwx917-soc", F)):
            b = await _build(session, p, BuildStatus.passed, t + timedelta(minutes=i),
                             profile=profile, commit_sha=sha(1))
            await _ran(session, b, {"t": outcome})
    await session.commit()

    body = (await client.get("/api/v1/analytics/flaky-tests")).json()
    assert body["enough_data"] is True
    assert body["tests"] == []
