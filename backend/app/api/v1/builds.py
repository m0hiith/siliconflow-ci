import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Maintainer
from app.db import get_session
from app.models import (
    Artifact,
    Build,
    BuildSize,
    BuildStage,
    BuildStatus,
    GateResult,
    Repository,
    RunReport,
    TestCase,
    TestOutcome,
)
from app.schemas import (
    ArtifactOut,
    BuildCreate,
    BuildDetailOut,
    BuildOut,
    BuildPage,
    BuildSizeCompareOut,
    BuildSizeOut,
    GateResultOut,
    LogSearchOut,
    PreviousPassingOut,
    StageLogOut,
    StageOut,
    TestCaseOut,
)
from app.services import classify, links, manual_builds, stage_logs
from app.services.artifacts import storage_path
from app.services.github import GitHubApp, GitHubError
from app.services.pagination import InvalidCursor, decode_cursor, encode_cursor

router = APIRouter(tags=["builds"])
log = logging.getLogger(__name__)

Session = Annotated[AsyncSession, Depends(get_session)]


def get_github(request: Request) -> GitHubApp:
    github: GitHubApp = request.app.state.github
    if not github.configured:
        raise HTTPException(status_code=503, detail="GitHub App not configured")
    return github


GitHub = Annotated[GitHubApp, Depends(get_github)]


async def get_build_or_404(session: AsyncSession, build_id: str) -> Build:
    build = await session.get(Build, build_id)
    if build is None:
        raise HTTPException(status_code=404, detail="build not found")
    return build


@router.get("/builds", response_model=BuildPage)
async def list_builds(
    session: Session,
    project_id: str | None = None,
    status: Annotated[list[BuildStatus] | None, Query()] = None,
    branch: str | None = None,
    profile: str | None = None,
    gh_run_id: Annotated[int | None, Query(ge=1, description="GitHub Actions run id")] = None,
    gh_run_attempt: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> BuildPage:
    """Builds, newest first. Repeat `status` to match any of several statuses.
    Pass `next_cursor` from the previous page as `cursor` to continue.
    `gh_run_id` and `gh_run_attempt` find the build of a workflow run attempt
    (ci/hil/fetch.py uses them)."""
    stmt = select(Build)
    if project_id is not None:
        stmt = stmt.where(Build.project_id == project_id)
    if status:
        stmt = stmt.where(Build.status.in_(status))
    if branch is not None:
        stmt = stmt.where(Build.branch == branch)
    if profile is not None:
        stmt = stmt.where(Build.profile == profile)
    if gh_run_id is not None:
        stmt = stmt.where(Build.gh_run_id == gh_run_id)
    if gh_run_attempt is not None:
        stmt = stmt.where(Build.gh_run_attempt == gh_run_attempt)
    if cursor is not None:
        try:
            created_at, row_id = decode_cursor(cursor)
        except InvalidCursor:
            raise HTTPException(status_code=400, detail="invalid cursor") from None
        stmt = stmt.where(tuple_(Build.created_at, Build.id) < tuple_(created_at, row_id))

    # Fetch one extra row to learn whether another page exists.
    stmt = stmt.order_by(Build.created_at.desc(), Build.id.desc()).limit(limit + 1)
    rows = list((await session.scalars(stmt)).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(rows[-1].created_at, rows[-1].id) if has_more else None
    return BuildPage(items=[BuildOut.model_validate(b) for b in rows], next_cursor=next_cursor)


def web_url(request: Request) -> str:
    return request.app.state.settings.github_web_url


@router.get("/builds/{build_id}", response_model=BuildDetailOut)
async def get_build(build_id: str, request: Request, session: Session) -> BuildDetailOut:
    build = await get_build_or_404(session, build_id)
    repo = await session.get_one(Repository, build.repository_id)
    report = await session.scalar(select(RunReport).where(RunReport.build_id == build_id))
    web = web_url(request)
    return BuildDetailOut(
        **BuildOut.model_validate(build).model_dump(),
        repository=repo.full_name,
        commit_url=links.commit_url(web, repo.full_name, build.commit_sha),
        run_url=links.run_url(web, repo.full_name, build.gh_run_id, build.gh_run_attempt),
        toolchain=(report.payload.get("toolchain") or {}) if report is not None else {},
        sdk=(report.payload.get("sdk") or {}) if report is not None else {},
        artifacts_state=report.artifacts_state if report is not None else None,
    )


@router.get("/builds/{build_id}/stages", response_model=list[StageOut])
async def list_stages(build_id: str, session: Session) -> list[BuildStage]:
    await get_build_or_404(session, build_id)
    stmt = (
        select(BuildStage)
        .where(BuildStage.build_id == build_id)
        .order_by(BuildStage.position, BuildStage.name)
    )
    return list((await session.scalars(stmt)).all())


async def load_stage_log(
    build_id: str, stage: str, request: Request, session: AsyncSession
) -> tuple[Build, BuildStage, list[str]]:
    build = await get_build_or_404(session, build_id)
    row = await session.scalar(
        select(BuildStage).where(BuildStage.build_id == build_id, BuildStage.name == stage)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="stage not found")
    if row.log_key is None or row.log_index is None:
        raise HTTPException(status_code=404, detail="no log stored for this stage")
    path = storage_path(request.app.state.settings.artifact_root, row.log_key)
    try:
        lines = await asyncio.to_thread(stage_logs.read_lines, path)
    except FileNotFoundError:
        log.error("stage log missing from the store", extra={"build_id": build_id, "stage": stage})
        raise HTTPException(status_code=404, detail="no log stored for this stage") from None
    return build, row, lines


LOG_RESPONSES = {404: {"description": "Build or stage not found, or no log stored for the stage"}}


@router.get("/builds/{build_id}/logs/{stage}", response_model=StageLogOut, responses=LOG_RESPONSES)
async def get_stage_log(
    build_id: str,
    stage: str,
    request: Request,
    session: Session,
    offset: Annotated[int, Query(ge=0, description="Lines to skip")] = 0,
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> StageLogOut:
    """A page of a stage's log (tokens redacted) and the index of the whole
    log. To start at error line n, pass offset=n-1."""
    build, row, lines = await load_stage_log(build_id, stage, request, session)
    index = row.log_index
    first_error = index["first_error"]
    if first_error is not None and first_error.get("file") and build.commit_sha:
        repo = await session.get_one(Repository, build.repository_id)
        path = links.repo_relative(first_error["file"], repo.full_name)
        if path is not None:
            first_error = {
                **first_error,
                "path": path,
                "url": links.blob_url(
                    web_url(request), repo.full_name, build.commit_sha, path, first_error.get("line")
                ),
            }
    return StageLogOut(
        stage=stage,
        offset=offset,
        total_lines=len(lines),
        lines=lines[offset : offset + limit],
        error_count=index["error_count"],
        warning_count=index["warning_count"],
        errors=index["errors"],
        warnings=index["warnings"],
        failure_class=index["failure_class"],
        first_error=first_error,
    )


@router.get(
    "/builds/{build_id}/logs/{stage}/search", response_model=LogSearchOut, responses=LOG_RESPONSES
)
async def search_stage_log(
    build_id: str,
    stage: str,
    request: Request,
    session: Session,
    q: Annotated[str, Query(min_length=1, max_length=200, description="Text to find")],
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
) -> LogSearchOut:
    """Lines of a stage's log containing `q`, ignoring case and ANSI colour codes."""
    _, _, lines = await load_stage_log(build_id, stage, request, session)
    needle = q.casefold()
    matches = [
        n for n, line in enumerate(lines, start=1) if needle in classify.strip_ansi(line).casefold()
    ]
    return LogSearchOut(stage=stage, query=q, total=len(matches), matches=matches[:limit])


@router.get(
    "/builds/{build_id}/previous-passing",
    response_model=PreviousPassingOut,
    responses={404: {"description": "Build not found"}},
)
async def previous_passing(build_id: str, request: Request, session: Session) -> PreviousPassingOut:
    """The last passing build before this one on the same repository, ref
    and profile, and a GitHub compare link between their commits (null when
    either commit is unknown or they are the same commit)."""
    build = await get_build_or_404(session, build_id)
    previous = await session.scalar(
        select(Build)
        .where(
            Build.project_id == build.project_id,
            Build.repository_id == build.repository_id,
            Build.ref == build.ref,
            Build.profile == build.profile,
            Build.status == BuildStatus.passed,
            Build.number < build.number,
        )
        .order_by(Build.number.desc())
        .limit(1)
    )
    compare_url = commits_between = None
    if (
        previous is not None
        and previous.commit_sha
        and build.commit_sha
        and previous.commit_sha != build.commit_sha
    ):
        repo = await session.get_one(Repository, build.repository_id)
        compare_url = links.compare_url(
            web_url(request), repo.full_name, previous.commit_sha, build.commit_sha
        )
        github: GitHubApp = request.app.state.github
        # Only for an installation already on record: a page view never
        # triggers an installation lookup.
        if github.configured and repo.gh_installation_id is not None:
            try:
                commits_between = await github.commits_ahead(
                    repo.gh_installation_id, repo.full_name, previous.commit_sha, build.commit_sha
                )
            except GitHubError as exc:
                log.warning("commit count unavailable", extra={"build_id": build_id, "error": str(exc)})
    return PreviousPassingOut(
        build=BuildOut.model_validate(previous) if previous is not None else None,
        compare_url=compare_url,
        commits_between=commits_between,
    )


@router.get(
    "/builds/{build_id}/size",
    response_model=BuildSizeCompareOut,
    responses={404: {"description": "Build not found"}},
)
async def build_size(build_id: str, session: Session) -> BuildSizeCompareOut:
    """The build's reported ELF size, and the size of the latest passing build
    before it on the repository's default branch with the same profile (the
    base to compare against). Pull request builds are compared with the
    default branch too, whatever their actual base branch."""
    build = await get_build_or_404(session, build_id)
    repo = await session.get_one(Repository, build.repository_id)
    size = await session.get(BuildSize, build_id)
    row = (
        await session.execute(
            select(Build, BuildSize)
            .join(BuildSize, BuildSize.build_id == Build.id)
            .where(
                Build.project_id == build.project_id,
                Build.repository_id == build.repository_id,
                Build.ref == f"refs/heads/{repo.default_branch}",
                Build.profile == build.profile,
                Build.status == BuildStatus.passed,
                Build.number < build.number,
            )
            .order_by(Build.number.desc())
            .limit(1)
        )
    ).first()
    return BuildSizeCompareOut(
        size=BuildSizeOut.model_validate(size) if size is not None else None,
        base_branch=repo.default_branch,
        base_build=BuildOut.model_validate(row[0]) if row else None,
        base_size=BuildSizeOut.model_validate(row[1]) if row else None,
    )


@router.get("/builds/{build_id}/tests", response_model=list[TestCaseOut])
async def list_tests(
    build_id: str,
    session: Session,
    outcome: Annotated[list[TestOutcome] | None, Query()] = None,
) -> list[TestCase]:
    await get_build_or_404(session, build_id)
    stmt = select(TestCase).where(TestCase.build_id == build_id)
    if outcome:
        stmt = stmt.where(TestCase.outcome.in_(outcome))
    stmt = stmt.order_by(TestCase.suite, TestCase.name)
    return list((await session.scalars(stmt)).all())


@router.get("/builds/{build_id}/gates", response_model=list[GateResultOut])
async def list_gate_results(build_id: str, session: Session) -> list[GateResult]:
    """Release gate results, in evaluation order. Empty until the gates are
    evaluated, which happens only for passed builds (docs/release-gates.md)."""
    await get_build_or_404(session, build_id)
    stmt = select(GateResult).where(GateResult.build_id == build_id).order_by(GateResult.id)
    return list((await session.scalars(stmt)).all())


@router.get("/builds/{build_id}/artifacts", response_model=list[ArtifactOut])
async def list_artifacts(build_id: str, session: Session) -> list[Artifact]:
    await get_build_or_404(session, build_id)
    stmt = (
        select(Artifact)
        .where(Artifact.build_id == build_id)
        .order_by(Artifact.kind, Artifact.file_name)
    )
    return list((await session.scalars(stmt)).all())


@router.post(
    "/projects/{project_id}/builds",
    status_code=201,
    response_model=BuildOut,
    responses={404: {"description": "Project not found"}, 503: {"description": "GitHub App not configured"}},
)
async def trigger_build(
    project_id: str,
    body: BuildCreate,
    request: Request,
    session: Session,
    user: Maintainer,
    github: GitHub,
) -> Build:
    """Start a manual build: record it as queued, then dispatch the workflow.

    If GitHub refuses the dispatch, the build is still created, as `errored`
    with the reason in `status_reason`."""
    try:
        return await manual_builds.trigger_manual_build(
            session,
            github,
            request.app.state.settings,
            project_id=project_id,
            repository_id=body.repository_id,
            branch=body.branch,
            user=user,
        )
    except manual_builds.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except manual_builds.InvalidRequest as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post(
    "/builds/{build_id}/cancel",
    status_code=202,
    response_model=BuildOut,
    responses={
        404: {"description": "Build not found"},
        409: {"description": "Build is finished or has no workflow run yet"},
        502: {"description": "GitHub refused or did not answer"},
        503: {"description": "GitHub App not configured"},
    },
)
async def cancel_build(build_id: str, session: Session, user: Maintainer, github: GitHub) -> Build:
    """Ask GitHub to cancel the build's run. The build's status is unchanged
    here; it becomes `cancelled` when GitHub's workflow_run webhook arrives."""
    try:
        return await manual_builds.request_cancel(session, github, build_id=build_id, user=user)
    except manual_builds.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except manual_builds.Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except manual_builds.UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
