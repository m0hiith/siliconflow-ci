"""services/transitions: the allowed-move table and the guarded UPDATE."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import TERMINAL_BUILD_STATUSES, Build, BuildStatus, Project
from app.services.transitions import ALLOWED_MOVES, can_transition, transition
from tests.factories import make_build, make_project

S = BuildStatus
T0 = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)

# queued < running < any terminal state. Status must never go down this order.
RANK = {S.queued: 0, S.running: 1, **{t: 2 for t in TERMINAL_BUILD_STATUSES}}


def test_allowed_moves_table():
    assert ALLOWED_MOVES[S.queued] == {S.running, S.cancelled, S.errored}
    assert ALLOWED_MOVES[S.running] == {S.passed, S.failed, S.cancelled, S.timed_out, S.errored}
    for terminal in TERMINAL_BUILD_STATUSES:
        assert ALLOWED_MOVES[terminal] == set()
    assert set(ALLOWED_MOVES) == set(BuildStatus)


@pytest.mark.parametrize("src", list(BuildStatus))
@pytest.mark.parametrize("dst", list(BuildStatus))
def test_no_move_goes_backwards_or_stays(src, dst):
    if can_transition(src, dst):
        assert RANK[dst] > RANK[src]


async def _status(session, build_id) -> tuple[BuildStatus, datetime | None, datetime | None]:
    row = (
        await session.execute(
            select(Build.status, Build.started_at, Build.finished_at).where(Build.id == build_id)
        )
    ).one()
    return row.status, row.started_at, row.finished_at


async def test_happy_path_sets_timestamps(session):
    build = await make_build(session, await make_project(session))
    await session.commit()

    assert await transition(session, build.id, S.running, T0)
    assert await transition(session, build.id, S.passed, T0 + timedelta(minutes=3), reason="ok")
    await session.commit()

    assert await _status(session, build.id) == (S.passed, T0, T0 + timedelta(minutes=3))
    await session.refresh(build)
    assert build.status is S.passed and build.status_reason == "ok"


async def test_rejected_moves_change_nothing(session):
    build = await make_build(session, await make_project(session))
    await session.commit()

    assert not await transition(session, build.id, S.passed, T0)  # queued -> passed: skips running
    assert not await transition(session, build.id, S.queued, T0)  # nothing moves *to* queued
    assert await transition(session, build.id, S.cancelled, T0)
    assert not await transition(session, build.id, S.running, T0)  # terminal is final
    assert not await transition(session, build.id, S.cancelled, T0)  # not even to itself
    await session.commit()
    assert await _status(session, build.id) == (S.cancelled, None, T0)


async def test_unknown_build_returns_false(session):
    assert not await transition(session, "bld_NOPE", S.running, T0)


async def test_finished_at_never_before_started_at(session):
    """GitHub's completion time can be earlier than a job's reported start."""
    build = await make_build(session, await make_project(session))
    await session.commit()
    assert await transition(session, build.id, S.running, T0)
    assert await transition(session, build.id, S.cancelled, T0 - timedelta(seconds=2))
    await session.commit()
    assert await _status(session, build.id) == (S.cancelled, T0, T0)


async def test_concurrent_terminal_moves_have_one_winner(sessionmaker):
    async with sessionmaker() as s:
        build = await make_build(s, await make_project(s))
        await transition(s, build.id, S.running, T0)
        await s.commit()

    async def move(to: BuildStatus) -> bool:
        async with sessionmaker() as s:
            changed = await transition(s, build.id, to, T0 + timedelta(minutes=1))
            await s.commit()
            return changed

    results = await asyncio.gather(move(S.passed), move(S.failed), move(S.cancelled))
    assert sorted(results) == [False, False, True]


@pytest.fixture
async def project_id(sessionmaker) -> str:
    async with sessionmaker() as s:
        project = await make_project(s)
        await s.commit()
        return project.id


@settings(
    max_examples=75,
    deadline=None,
    # Each example creates its own build; the shared fixtures are only the
    # database and a project, which examples do not modify.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(events=st.lists(st.sampled_from(list(BuildStatus)), max_size=12))
async def test_random_event_orders_never_regress(sessionmaker, project_id, events):
    async with sessionmaker() as s:
        project = await s.get_one(
            Project, project_id, options=[selectinload(Project.repositories)]
        )
        build = await make_build(s, project)
        await s.commit()

        status, ever_ran = S.queued, False
        for i, to in enumerate(events):
            at = T0 + timedelta(seconds=i)
            changed = await transition(s, build.id, to, at)
            await s.commit()
            new_status, started_at, finished_at = await _status(s, build.id)

            # The DB agrees with the table, and the return value tells the truth.
            assert changed == can_transition(status, to)
            assert new_status == (to if changed else status)
            # Never backwards, never out of a terminal state.
            assert RANK[new_status] >= RANK[status]
            if status in TERMINAL_BUILD_STATUSES:
                assert new_status == status
            # Timestamps follow the status.
            ever_ran |= new_status is S.running
            assert (started_at is not None) == ever_ran
            assert (finished_at is not None) == (new_status in TERMINAL_BUILD_STATUSES)
            status = new_status
