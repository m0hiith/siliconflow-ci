from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ids import new_id
from app.models import Build, BuildStatus, BuildTrigger, Project


class ProjectNotFound(LookupError):
    pass


async def allocate_build_number(session: AsyncSession, project_id: str) -> int:
    """Reserve the next build number for a project.

    The UPDATE takes a row lock on the project that is held until the caller's
    transaction ends, so concurrent allocations for one project serialise and
    each sees the previous one's increment. Projects do not contend with each
    other. If the transaction rolls back, the number is released with it, so
    numbers stay gapless.
    """
    result = await session.execute(
        update(Project)
        .where(Project.id == project_id)
        .values(next_build_number=Project.next_build_number + 1)
        .returning(Project.next_build_number - 1)
    )
    number = result.scalar_one_or_none()
    if number is None:
        raise ProjectNotFound(project_id)
    return number


async def create_build(
    session: AsyncSession,
    *,
    project_id: str,
    repository_id: str,
    trigger: BuildTrigger,
    profile: str,
    ref: str,
    **fields: object,
) -> Build:
    """Insert a queued build with the next per-project number. The caller commits."""
    number = await allocate_build_number(session, project_id)
    build = Build(
        id=new_id("bld"),
        project_id=project_id,
        repository_id=repository_id,
        number=number,
        status=BuildStatus.queued,
        trigger=trigger,
        profile=profile,
        ref=ref,
        **fields,
    )
    session.add(build)
    await session.flush()
    return build
