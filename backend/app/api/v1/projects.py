from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.models import Build, BuildSize, Project
from app.schemas import ProjectOut, SizePointOut

router = APIRouter(tags=["projects"])

Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(session: Session) -> list[Project]:
    stmt = select(Project).options(selectinload(Project.repositories)).order_by(Project.slug)
    return list((await session.scalars(stmt)).all())


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: str, session: Session) -> Project:
    project = await session.get(
        Project, project_id, options=[selectinload(Project.repositories)]
    )
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.get(
    "/projects/{project_id}/sizes",
    response_model=list[SizePointOut],
    responses={404: {"description": "Project not found"}},
)
async def list_sizes(
    project_id: str,
    session: Session,
    branch: str | None = None,
    profile: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[SizePointOut]:
    """ELF sizes reported by the project's builds, newest build first. Each
    build of any status that reported a size is listed once."""
    if await session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project not found")
    stmt = (
        select(Build, BuildSize)
        .join(BuildSize, BuildSize.build_id == Build.id)
        .where(Build.project_id == project_id)
    )
    if branch is not None:
        stmt = stmt.where(Build.branch == branch)
    if profile is not None:
        stmt = stmt.where(Build.profile == profile)
    stmt = stmt.order_by(Build.created_at.desc(), Build.id.desc()).limit(limit)
    return [
        SizePointOut(
            build_id=build.id,
            build_number=build.number,
            status=build.status,
            profile=build.profile,
            branch=build.branch,
            commit_sha=build.commit_sha,
            created_at=build.created_at,
            file_name=size.file_name,
            text_bytes=size.text_bytes,
            data_bytes=size.data_bytes,
            bss_bytes=size.bss_bytes,
        )
        for build, size in (await session.execute(stmt)).all()
    ]
