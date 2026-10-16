"""Manual builds and cancellation: the two places SiliconFlow asks GitHub to act.

A manual build is committed as `queued` *before* GitHub is called, so the
project row lock taken by build numbering is never held across a network
call, and the run that GitHub starts always finds its build. The workflow is
dispatched with input build_id; its run name "sfci <build_id>" is how the
first workflow_run webhook binds the run to the build (services.github_events).

If the dispatch fails, the build moves to `errored` with the reason. A
dispatch that timed out may still have started a run on GitHub; that run is
bound to the build by name as usual, but the build stays `errored`, since
terminal states never change.

Cancellation only asks GitHub. The build becomes `cancelled` when the
workflow_run "completed" webhook says so.
"""
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    TERMINAL_BUILD_STATUSES,
    AuditLog,
    Build,
    BuildStatus,
    BuildTrigger,
    Project,
    Repository,
    User,
)
from app.services.builds import create_build
from app.services.github import GitHubApp, GitHubError
from app.services.github_events import workflow_profile
from app.services.transitions import transition

log = logging.getLogger(__name__)


class NotFound(LookupError):
    pass


class InvalidRequest(ValueError):
    pass


class Conflict(Exception):
    pass


class UpstreamError(Exception):
    pass


async def _pick_repository(session: AsyncSession, project_id: str, repository_id: str | None) -> Repository:
    if await session.get(Project, project_id) is None:
        raise NotFound("project not found")
    repos = list(
        (await session.scalars(select(Repository).where(Repository.project_id == project_id))).all()
    )
    if repository_id is not None:
        for repo in repos:
            if repo.id == repository_id:
                return repo
        raise InvalidRequest("repository does not belong to this project")
    if len(repos) != 1:
        raise InvalidRequest(
            "project has no repository" if not repos else "project has several repositories; pass repository_id"
        )
    return repos[0]


async def _installation_id(session: AsyncSession, github: GitHubApp, repo: Repository) -> int:
    """The repository's App installation id, looked up once and then stored."""
    if repo.gh_installation_id is None:
        repo.gh_installation_id = await github.repository_installation_id(repo.full_name)
        await session.commit()
    return repo.gh_installation_id


async def trigger_manual_build(
    session: AsyncSession,
    github: GitHubApp,
    settings: Settings,
    *,
    project_id: str,
    repository_id: str | None,
    branch: str | None,
    user: User,
) -> Build:
    repo = await _pick_repository(session, project_id, repository_id)
    branch = branch or repo.default_branch
    build = await create_build(
        session,
        project_id=project_id,
        repository_id=repo.id,
        trigger=BuildTrigger.manual,
        profile=workflow_profile(settings.github_workflow_path),
        ref=f"refs/heads/{branch}",
        branch=branch,
        triggered_by_user_id=user.id,
    )
    session.add(
        AuditLog(
            actor_user_id=user.id,
            action="build.trigger",
            target_type="build",
            target_id=build.id,
            details={
                "project_id": project_id,
                "repository": repo.full_name,
                "branch": branch,
                "workflow": settings.github_workflow_path,
            },
        )
    )
    await session.commit()

    extra = {"build_id": build.id, "repository": repo.full_name}
    try:
        installation_id = await _installation_id(session, github, repo)
        await github.dispatch_workflow(
            installation_id,
            repo.full_name,
            settings.github_workflow_path.rsplit("/", 1)[-1],
            ref=branch,
            inputs={"build_id": build.id},
        )
    except GitHubError as exc:
        log.warning("workflow dispatch failed", extra={**extra, "error": str(exc)})
        await transition(
            session,
            build.id,
            BuildStatus.errored,
            datetime.now(UTC),
            reason=f"workflow dispatch failed: {exc}",
        )
        await session.commit()
        return build

    log.info("workflow dispatched", extra=extra)
    return build


async def request_cancel(session: AsyncSession, github: GitHubApp, *, build_id: str, user: User) -> Build:
    build = await session.get(Build, build_id)
    if build is None:
        raise NotFound("build not found")
    if build.status in TERMINAL_BUILD_STATUSES:
        raise Conflict(f"build is already {build.status.value}")
    if build.gh_run_id is None:
        raise Conflict("build has no workflow run yet; retry once it has started")
    repo = await session.get(Repository, build.repository_id)
    # End the read transaction before calling GitHub.
    await session.commit()

    try:
        installation_id = await _installation_id(session, github, repo)
        await github.cancel_run(installation_id, repo.full_name, build.gh_run_id)
    except GitHubError as exc:
        log.warning("run cancel failed", extra={"build_id": build.id, "error": str(exc)})
        if exc.status == 409:
            raise Conflict("GitHub cannot cancel this run; it has probably finished") from exc
        raise UpstreamError(str(exc)) from exc

    session.add(
        AuditLog(
            actor_user_id=user.id,
            action="build.cancel_request",
            target_type="build",
            target_id=build.id,
            details={"gh_run_id": build.gh_run_id, "gh_run_attempt": build.gh_run_attempt},
        )
    )
    await session.commit()
    log.info("run cancel requested", extra={"build_id": build.id, "run_id": build.gh_run_id})
    return build
