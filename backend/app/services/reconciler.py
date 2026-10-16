"""Repair builds whose webhooks never arrived.

A webhook delivery can be lost outright (the backend was down past GitHub's
retries, the secret was rotated, the event was deleted). A build that missed
its "completed" event would stay queued or running forever, so the worker
runs a reconcile pass every settings.reconcile_interval_s.

A pass looks for builds still queued or running more than
settings.build_timeout_s + settings.reconcile_grace_s after they started (or
were queued, if they never started). For each, it fetches the run attempt
from the GitHub API and applies what GitHub says, exactly as the webhook
would have:

  completed      github_events.apply_conclusion, so success and failure
                 still wait for the run report;
  in_progress    a queued build moves to running;
  anything else  nothing to correct yet; the next pass looks again.

Builds with a success or failure conclusion are waiting for their run
report, and the report deadline job settles them; GitHub has nothing more to
say about them. If one is still active settings.report_deadline_s +
settings.reconcile_grace_s after its last change, its deadline job never
ran to completion (it failed permanently), so the pass does that job's work
itself (outcome.handle_deadline). Builds awaiting approval (action_required)
are fetched like any other, since the run continues once approved.

A manual build is bound to its run by the run's first webhook. One that is
still unbound settings.build_timeout_s + settings.reconcile_grace_s after it
was queued never got a run (the API stopped between committing the build
and dispatching it, or GitHub never started the run), and nothing would ever
move it: it is errored.

The GitHub call is made outside any transaction. The correction then takes
the run's advisory lock, like webhook handling, and re-reads the build, so a
webhook that lands in between is never undone. Every correction is logged at
WARNING, since each one means a webhook or a job went missing.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Build, BuildStatus, BuildTrigger, Repository
from app.services import outcome
from app.services.github import GitHubApp
from app.services.github_events import apply_conclusion, clear_awaiting_approval, parse_ts
from app.services.transitions import transition

log = logging.getLogger(__name__)

ACTIVE = (BuildStatus.queued, BuildStatus.running)
WAITING_FOR_REPORT = Build.gh_conclusion.in_(outcome.DECIDED_BY_REPORT)


@dataclass(frozen=True)
class Candidate:
    build_id: str
    run_id: int
    attempt: int
    repository_id: str
    full_name: str
    installation_id: int | None


async def overdue_builds(session: AsyncSession, settings: Settings) -> list[Candidate]:
    """Active builds past their timeout plus grace, oldest first."""
    since = func.coalesce(Build.started_at, Build.queued_at)
    cutoff = func.now() - timedelta(seconds=settings.build_timeout_s + settings.reconcile_grace_s)
    rows = await session.execute(
        select(
            Build.id,
            Build.gh_run_id,
            Build.gh_run_attempt,
            Repository.id,
            Repository.full_name,
            Repository.gh_installation_id,
        )
        .join(Repository, Repository.id == Build.repository_id)
        .where(
            Build.status.in_(ACTIVE),
            Build.gh_run_id.is_not(None),
            or_(Build.gh_conclusion.is_(None), ~WAITING_FOR_REPORT),
            since < cutoff,
        )
        .order_by(since, Build.id)
    )
    return [Candidate(*row) for row in rows]


async def past_report_deadline(session: AsyncSession, settings: Settings) -> list[str]:
    """Active builds waiting for a report whose deadline job should long have run."""
    cutoff = func.now() - timedelta(seconds=settings.report_deadline_s + settings.reconcile_grace_s)
    return list(
        await session.scalars(
            select(Build.id)
            .where(Build.status.in_(ACTIVE), WAITING_FOR_REPORT, Build.updated_at < cutoff)
            .order_by(Build.updated_at, Build.id)
        )
    )


async def unbound_manual_builds(session: AsyncSession, settings: Settings) -> list[str]:
    cutoff = func.now() - timedelta(seconds=settings.build_timeout_s + settings.reconcile_grace_s)
    return list(
        await session.scalars(
            select(Build.id)
            .where(
                Build.status == BuildStatus.queued,
                Build.trigger == BuildTrigger.manual,
                Build.gh_run_id.is_(None),
                Build.queued_at < cutoff,
            )
            .order_by(Build.queued_at, Build.id)
        )
    )


async def settle_past_deadline(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, build_id: str
) -> bool:
    """Do a lost deadline job's work. Returns whether the build finished."""
    async with sessionmaker() as session, session.begin():
        await outcome.handle_deadline(session, build_id, settings)
        build = await _load(session, build_id)
        if build.status in ACTIVE:
            return False
        log.warning(
            "build settled after its report deadline job was lost",
            extra={"build_id": build_id, "to_status": build.status.value, "reason": build.status_reason},
        )
        return True


async def error_unbound(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, build_id: str
) -> bool:
    """Error a manual build that never got a run. Returns whether it did."""
    async with sessionmaker() as session, session.begin():
        # Locked like the binding in github_events.find_or_create_build, so a
        # run that binds it now wins or waits.
        build = await session.scalar(select(Build).where(Build.id == build_id).with_for_update())
        if build is None or build.status is not BuildStatus.queued or build.gh_run_id is not None:
            return False
        minutes = (settings.build_timeout_s + settings.reconcile_grace_s) / 60
        moved = await transition(
            session,
            build_id,
            BuildStatus.errored,
            datetime.now(UTC),
            reason=f"no workflow run started for this manual build within {minutes:g} minutes",
        )
        if moved:
            log.warning("unbound manual build errored", extra={"build_id": build_id})
        return moved


async def reconcile(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    github: GitHubApp,
    heartbeat: Callable[[], None] = lambda: None,
) -> int:
    """One reconcile pass. Returns how many builds it corrected."""
    async with sessionmaker() as session:
        candidates = await overdue_builds(session, settings)
        waiting = await past_report_deadline(session, settings)
        unbound = await unbound_manual_builds(session, settings)
    corrected = 0
    local = [(settle_past_deadline, b) for b in waiting] + [(error_unbound, b) for b in unbound]
    for fix, build_id in local:
        try:
            if await fix(sessionmaker, settings, build_id):
                corrected += 1
        except Exception as exc:
            log.warning(
                "build reconcile failed",
                extra={"build_id": build_id, "error": f"{type(exc).__name__}: {exc}"},
            )
        heartbeat()
    installations: dict[str, int] = {}
    for candidate in candidates:
        try:
            if await reconcile_build(sessionmaker, settings, github, candidate, installations):
                corrected += 1
        except Exception as exc:
            # One unreachable run must not stop the pass; the next pass retries it.
            log.warning(
                "build reconcile failed",
                extra={
                    "build_id": candidate.build_id,
                    "run_id": candidate.run_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        heartbeat()
    if checked := len(candidates) + len(local):
        log.info("reconcile pass done", extra={"checked": checked, "corrected": corrected})
    return corrected


async def reconcile_build(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    github: GitHubApp,
    c: Candidate,
    installations: dict[str, int],
) -> bool:
    """Fetch one build's run attempt and apply it. Returns whether the build changed."""
    installation_id = c.installation_id or installations.get(c.repository_id)
    if installation_id is None:
        installation_id = await github.repository_installation_id(c.full_name)
    installations[c.repository_id] = installation_id
    run = await github.get_run_attempt(installation_id, c.full_name, c.run_id, c.attempt)

    async with sessionmaker() as session, session.begin():
        if c.installation_id is None:
            await session.execute(
                update(Repository)
                .where(Repository.id == c.repository_id, Repository.gh_installation_id.is_(None))
                .values(gh_installation_id=installation_id)
            )
        await session.execute(select(func.pg_advisory_xact_lock(c.run_id)))
        build = await _load(session, c.build_id)
        if (
            build is None
            or build.status not in ACTIVE
            or build.gh_conclusion in outcome.DECIDED_BY_REPORT
        ):
            return False  # a webhook got here first
        before = (build.status, build.gh_conclusion)

        await apply_run(session, build, run, settings)

        build = await _load(session, c.build_id)
        if (build.status, build.gh_conclusion) == before:
            log.debug(
                "build checked, nothing to correct",
                extra={"build_id": c.build_id, "run_id": c.run_id, "gh_status": run.get("status")},
            )
            return False
        log.warning(
            "build corrected from the GitHub API",
            extra={
                "build_id": c.build_id,
                "run_id": c.run_id,
                "attempt": c.attempt,
                "from_status": before[0].value,
                "to_status": build.status.value,
                "gh_status": run.get("status"),
                "gh_conclusion": run.get("conclusion"),
                "reason": build.status_reason,
            },
        )
        return True


async def apply_run(
    session: AsyncSession, build: Build, run: dict[str, Any], settings: Settings
) -> None:
    status = run.get("status")
    if status == "completed":
        await apply_conclusion(session, build, run, settings)
    elif status == "in_progress":
        await clear_awaiting_approval(session, build)
        started = parse_ts(run.get("run_started_at") or run["created_at"])
        await transition(session, build.id, BuildStatus.running, started)


async def _load(session: AsyncSession, build_id: str) -> Build | None:
    # populate_existing: the transitions above may have moved it.
    return await session.scalar(
        select(Build).where(Build.id == build_id).execution_options(populate_existing=True)
    )
