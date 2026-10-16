"""Helpers that insert rows for tests. Builds go through the real create_build
service so numbering is exercised everywhere."""
from datetime import datetime
from itertools import count

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Build, BuildTrigger, Project, Repository
from app.services.builds import create_build

_seq = count(1)


async def make_project(session: AsyncSession, slug: str | None = None) -> Project:
    n = next(_seq)
    slug = slug or f"project-{n}"
    project = Project(slug=slug, name=slug.title())
    session.add(project)
    await session.flush()
    session.add(Repository(project_id=project.id, full_name=f"acme/{slug}"))
    await session.flush()
    await session.refresh(project, ["repositories"])
    return project


async def make_build(
    session: AsyncSession,
    project: Project,
    *,
    trigger: BuildTrigger = BuildTrigger.push,
    profile: str = "host-cmake",
    branch: str = "main",
    created_at: datetime | None = None,
    **fields,
) -> Build:
    if created_at is not None:
        fields["created_at"] = created_at
    return await create_build(
        session,
        project_id=project.id,
        repository_id=project.repositories[0].id,
        trigger=trigger,
        profile=profile,
        ref=f"refs/heads/{branch}",
        branch=branch,
        **fields,
    )
