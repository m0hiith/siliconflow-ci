"""Per-project build numbers stay unique and gapless under concurrency."""
import asyncio

import pytest

from app.models import BuildTrigger
from app.services.builds import ProjectNotFound, allocate_build_number, create_build
from tests.factories import make_build, make_project


async def test_numbers_are_sequential_per_project(session):
    a = await make_project(session)
    b = await make_project(session)
    numbers_a = [(await make_build(session, a)).number for _ in range(3)]
    numbers_b = [(await make_build(session, b)).number for _ in range(2)]
    await session.commit()
    assert numbers_a == [1, 2, 3]
    assert numbers_b == [1, 2]


async def test_concurrent_creation_yields_unique_contiguous_numbers(session, sessionmaker):
    project = await make_project(session)
    await session.commit()
    repo_id = project.repositories[0].id

    async def create_one() -> int:
        async with sessionmaker() as s:
            build = await create_build(
                s,
                project_id=project.id,
                repository_id=repo_id,
                trigger=BuildTrigger.push,
                profile="host-cmake",
                ref="refs/heads/main",
            )
            await asyncio.sleep(0.01)  # hold the lock briefly to force overlap
            await s.commit()
            return build.number

    n = 25
    numbers = await asyncio.gather(*(create_one() for _ in range(n)))
    assert sorted(numbers) == list(range(1, n + 1))


async def test_second_allocation_waits_for_first_transaction(session, sessionmaker):
    """The project row lock serialises allocations: B blocks until A commits."""
    project = await make_project(session)
    await session.commit()

    async with sessionmaker() as a, sessionmaker() as b:
        assert await allocate_build_number(a, project.id) == 1

        b_task = asyncio.create_task(allocate_build_number(b, project.id))
        await asyncio.sleep(0.3)
        assert not b_task.done(), "second allocation should be blocked by the row lock"

        await a.commit()
        assert await asyncio.wait_for(b_task, timeout=5) == 2
        await b.commit()


async def test_rolled_back_allocation_releases_the_number(session, sessionmaker):
    project = await make_project(session)
    await session.commit()

    async with sessionmaker() as s:
        assert await allocate_build_number(s, project.id) == 1
        await s.rollback()

    async with sessionmaker() as s:
        assert await allocate_build_number(s, project.id) == 1
        await s.commit()


async def test_unknown_project_raises(session):
    with pytest.raises(ProjectNotFound):
        await allocate_build_number(session, "prj_DOESNOTEXIST")
