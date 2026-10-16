"""Decide a build's final status from its run report and its run's conclusion.

For runs that GitHub concludes "success" or "failure":

  passed   the conclusion is "success", the run report arrived, and every
           required stage passed;
  failed   the report arrived and anything else is true (a required stage
           failed or never ran, or the run failed outside the stages);
  errored  no report arrived within settings.report_deadline_s of the
           conclusion. Without the report nothing says what was built or
           tested, so the CI infrastructure failed, not the firmware.
           Exception: a re-run attempt whose reporting job was reused from
           an earlier attempt inherits that attempt's report
           (inherit_report).

The report is posted from inside the job, so it normally arrives before the
workflow_run "completed" webhook; whichever comes second settles the build.
Both callers hold the run's advisory lock (see services.github_events), so
they cannot miss each other.

cancelled, timed_out and startup_failure are settled by the webhook alone.
A build that passes gets its release gates evaluated (services.gates).
"""
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    TERMINAL_BUILD_STATUSES,
    AuditLog,
    Build,
    BuildSize,
    BuildStage,
    BuildStatus,
    Job,
    RunReport,
    StageStatus,
)
from app.services import gates
from app.services.artifacts import INGEST_JOB_KIND
from app.services.transitions import transition

log = logging.getLogger(__name__)

DECIDED_BY_REPORT = frozenset({"success", "failure"})
DEADLINE_JOB_KIND = "report_deadline"


@dataclass(frozen=True)
class StageFact:
    name: str
    required: bool
    status: StageStatus


def decide(conclusion: str, stages: Sequence[StageFact]) -> tuple[BuildStatus, str]:
    """The final status for a run whose report arrived. Pure, for testing."""
    if conclusion not in DECIDED_BY_REPORT:
        raise ValueError(f"conclusion {conclusion!r} is not decided by the run report")
    not_passed = [s for s in stages if s.required and s.status is not StageStatus.passed]
    if not_passed:
        listed = ", ".join(f"{s.name} ({s.status.value})" for s in not_passed)
        return BuildStatus.failed, f"required stages did not pass: {listed}"
    if conclusion == "success":
        return BuildStatus.passed, "workflow run succeeded and every required stage passed"
    return BuildStatus.failed, "workflow run failed outside the required stages"


async def _load_build(session: AsyncSession, build_id: str) -> Build:
    # populate_existing: another statement in this transaction may have moved it.
    return await session.scalar(
        select(Build).where(Build.id == build_id).execution_options(populate_existing=True)
    )


async def has_report(session: AsyncSession, build_id: str) -> bool:
    return bool(await session.scalar(select(exists().where(RunReport.build_id == build_id))))


async def settle(session: AsyncSession, build_id: str, at: datetime) -> bool:
    """Give the build its final status if the report and the conclusion are
    both in. Returns whether the build is finished (now or already)."""
    build = await _load_build(session, build_id)
    if build.status in TERMINAL_BUILD_STATUSES:
        return True
    if build.gh_conclusion not in DECIDED_BY_REPORT or not await has_report(session, build_id):
        return False

    rows = await session.execute(
        select(BuildStage.name, BuildStage.required, BuildStage.status)
        .where(BuildStage.build_id == build_id)
        .order_by(BuildStage.position)
    )
    status, reason = decide(build.gh_conclusion, [StageFact(*row) for row in rows])
    if build.status is BuildStatus.queued:
        # The job ran even if its workflow_job webhook never arrived.
        await transition(session, build_id, BuildStatus.running, at)
    moved = await transition(session, build_id, status, at, reason=reason)
    if moved and status is BuildStatus.passed:
        await gates.queue_evaluation(session, build_id)
    return moved


async def schedule_deadline(session: AsyncSession, build_id: str, settings: Settings) -> None:
    await session.execute(
        insert(Job).values(
            kind=DEADLINE_JOB_KIND,
            payload={"build_id": build_id},
            run_after=func.now() + timedelta(seconds=settings.report_deadline_s),
        )
    )


async def inherit_report(session: AsyncSession, build: Build) -> bool:
    """Give a re-run attempt the latest earlier attempt's run report, if every
    required stage in it passed. Returns whether it did.

    "Re-run failed jobs" reuses the jobs that succeeded, so when only another
    job failed, the job that posts the report does not run again and the new
    attempt gets no report of its own. That job passed every required stage,
    at the same commit, so its report still describes the firmware. The
    artifacts are fetched again for this build from the same run."""
    if not build.gh_run_attempt or build.gh_run_attempt < 2:
        return False
    source = (
        await session.execute(
            select(RunReport, Build.id, Build.gh_run_attempt)
            .join(Build, Build.id == RunReport.build_id)
            .where(Build.gh_run_id == build.gh_run_id, Build.gh_run_attempt < build.gh_run_attempt)
            .order_by(Build.gh_run_attempt.desc())
            .limit(1)
        )
    ).first()
    if source is None:
        return False
    report, source_id, source_attempt = source
    stages = (
        await session.scalars(
            select(BuildStage).where(BuildStage.build_id == source_id).order_by(BuildStage.position)
        )
    ).all()
    if not any(s.required for s in stages) or any(
        s.required and s.status is not StageStatus.passed for s in stages
    ):
        return False

    fetch = report.payload.get("artifact_name") is not None
    session.add(
        RunReport(
            build_id=build.id,
            schema_version=report.schema_version,
            payload=report.payload,
            digest=report.digest,
            claims=report.claims,
            artifacts_state="pending" if fetch else "none",
        )
    )
    await session.execute(
        insert(BuildStage),
        [
            {
                "build_id": build.id,
                "name": s.name,
                "position": s.position,
                "required": s.required,
                "status": s.status,
                "exit_code": s.exit_code,
                "started_at": s.started_at,
                "finished_at": s.finished_at,
            }
            for s in stages
        ],
    )
    if (size := await session.get(BuildSize, source_id)) is not None:
        session.add(
            BuildSize(
                build_id=build.id,
                file_name=size.file_name,
                text_bytes=size.text_bytes,
                data_bytes=size.data_bytes,
                bss_bytes=size.bss_bytes,
                sections=size.sections,
            )
        )
    if fetch:
        await session.execute(insert(Job).values(kind=INGEST_JOB_KIND, payload={"build_id": build.id}))
    session.add(
        AuditLog(
            action="run_report.inherited",
            target_type="build",
            target_id=build.id,
            details={"from_build_id": source_id, "from_attempt": source_attempt},
        )
    )
    await session.flush()
    log.info(
        "run report inherited from an earlier attempt",
        extra={"build_id": build.id, "from_build_id": source_id, "from_attempt": source_attempt},
    )
    return True


async def handle_deadline(session: AsyncSession, build_id: str, settings: Settings) -> None:
    """The deadline job: settle the build, or error it if the report never came."""
    build = await _load_build(session, build_id)
    if build is None:
        return
    await session.execute(select(func.pg_advisory_xact_lock(build.gh_run_id)))
    now = datetime.now(UTC)
    if await settle(session, build_id, now):
        return
    build = await _load_build(session, build_id)
    if build.gh_conclusion not in DECIDED_BY_REPORT or await has_report(session, build_id):
        return
    if await inherit_report(session, build):
        await settle(session, build_id, now)
        return
    minutes = settings.report_deadline_s / 60
    await transition(
        session,
        build_id,
        BuildStatus.errored,
        now,
        reason=(
            f"workflow run concluded {build.gh_conclusion} but no run report arrived "
            f"within {minutes:g} minutes"
        ),
    )
