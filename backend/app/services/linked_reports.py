"""Linked run reports: a workflow run that reports on another run's build.

The hardware-in-the-loop workflow (hil.yml, docs/hil.md) runs after a
siwx917-soc build's workflow and tests that build's image on a real board. It
has no build of its own: its run report names the build it tested
(`build_id`) and its own profile (`profile`, e.g. siwx917-hil), and adds one
stage, "hil", to that build.

Who may link to a build:

  - the OIDC token comes from a workflow in settings.github_hil_workflows, in
    the build's repository, started by workflow_run, which always runs the
    default branch's copy of the workflow (its workflow_ref must say so);
  - that workflow lists the report's profile, and the build has the profile
    it tests;
  - the build is not a pull request build, and it passed.

A workflow_run token's sha is the default branch's head, not the commit under
test, so unlike a build's own report the commit cannot be matched against the
token. What ties the run to the build is that the default branch's workflow
picked it, and the board's boot line, which ci/hil/boot_check.py checks
against the build's commit.

The stage is never required: the build's status was decided by its own report
and never changes here. It passed when every reported stage passed and
fetch, flash and test all ran. Its exit code is the first unpassed stage's,
and exit code 75 classifies it infra_hw (classify.exit_code_index).

One linked report per build and profile. An identical retry is a duplicate,
and a later attempt of the same run replaces it (re-running after an
infrastructure failure). Anything else is refused with 409.
"""
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    AuditLog,
    Build,
    BuildStage,
    BuildStatus,
    BuildTrigger,
    LinkedReport,
    Repository,
    StageStatus,
)
from app.schemas import ReportStage, RunReportIn
from app.services import classify
from app.services.oidc import RunClaims
from app.services.run_reports import Accepted, ReportRejected, digest_of, payload_of

log = logging.getLogger(__name__)

HIL_STAGE = "hil"
# ci/hil's stages, as hil.yml runs them; one that never ran fails the stage.
HIL_REQUIRED_STAGES = ("fetch", "flash", "test")


@dataclass(frozen=True)
class StageSummary:
    status: StageStatus
    exit_code: int | None
    started_at: datetime | None
    finished_at: datetime | None


def summarize(stages: list[ReportStage]) -> StageSummary:
    """The one build stage a linked report's stages add up to. Pure."""
    reported = {s.name for s in stages}
    unpassed = [s for s in stages if s.status != "passed"]
    missing = [name for name in HIL_REQUIRED_STAGES if name not in reported]
    if unpassed:
        status, exit_code = StageStatus.failed, unpassed[0].exit_code
    elif missing:
        status, exit_code = StageStatus.failed, None
    else:
        status, exit_code = StageStatus.passed, 0
    return StageSummary(
        status=status,
        exit_code=exit_code,
        started_at=min((s.started_at for s in stages if s.started_at), default=None),
        finished_at=max((s.finished_at for s in stages if s.finished_at), default=None),
    )


def check_identity(claims: RunClaims, report: RunReportIn, settings: Settings) -> tuple[str, str]:
    """The body must describe the token's run, and the token must come from a
    hardware-in-the-loop workflow that reports this profile. Returns the
    profile of the builds it tests and the ref the workflow file was read at."""
    gh = report.github
    if (
        gh.repository.lower() != claims.repository.lower()
        or (gh.run_id, gh.run_attempt, gh.sha) != (claims.run_id, claims.run_attempt, claims.sha)
    ):
        raise ReportRejected(403, "report does not describe the run its token was issued to")
    workflow, _, ref = claims.workflow_ref.partition("@")
    prefix = f"{claims.repository}/".lower()
    profiles = None
    if workflow.lower().startswith(prefix):
        profiles = settings.hil_profiles_for(workflow[len(prefix):])
    if profiles is None:
        raise ReportRejected(403, "token is not from a hardware-in-the-loop workflow")
    if claims.event_name != "workflow_run":
        raise ReportRejected(403, "hardware-in-the-loop reports must come from a workflow_run run")
    tested = profiles.get(report.profile or "")
    if tested is None:
        raise ReportRejected(403, f"this workflow does not report profile {report.profile!r}")
    return tested, ref


def check_build(build: Build, repo: Repository, claims: RunClaims, tested: str, ref: str) -> None:
    if repo.full_name.lower() != claims.repository.lower():
        raise ReportRejected(403, "token repository does not match the build")
    if repo.gh_repo_id is not None and claims.repository_id is not None:
        if str(repo.gh_repo_id) != claims.repository_id:
            raise ReportRejected(403, "token repository id does not match the build")
    if ref != f"refs/heads/{repo.default_branch}":
        raise ReportRejected(403, "the workflow did not run from the default branch")
    if build.trigger is BuildTrigger.pull_request:
        raise ReportRejected(403, "pull request builds are not tested on hardware")
    if build.profile != tested:
        raise ReportRejected(403, f"this profile tests {tested} builds, not {build.profile}")


async def accept_linked_report(
    session: AsyncSession, claims: RunClaims, report: RunReportIn, settings: Settings
) -> Accepted:
    """Store a verified linked report and set its stage. The caller commits."""
    tested, ref = check_identity(claims, report, settings)
    build = await session.get(Build, report.build_id)
    if build is None:
        raise ReportRejected(404, "no such build")
    repo = await session.get_one(Repository, build.repository_id)
    check_build(build, repo, claims, tested, ref)

    # Serialised with the build's own run report and webhooks.
    if build.gh_run_id is not None:
        await session.execute(select(func.pg_advisory_xact_lock(build.gh_run_id)))
    build = await session.scalar(
        select(Build).where(Build.id == build.id).execution_options(populate_existing=True)
    )
    if build.status is not BuildStatus.passed:
        raise ReportRejected(409, f"build is {build.status.value}, not passed")

    payload = payload_of(report)
    digest = digest_of(payload)
    existing = await session.scalar(
        select(LinkedReport)
        .where(LinkedReport.build_id == build.id, LinkedReport.profile == report.profile)
        .with_for_update()
    )
    if existing is not None:
        if existing.digest == digest:
            return Accepted(build.id, duplicate=True)
        if existing.gh_run_id != claims.run_id or existing.gh_run_attempt >= claims.run_attempt:
            raise ReportRejected(409, f"a different {report.profile} report was already recorded for this build")
    elif await session.scalar(
        select(BuildStage.id).where(BuildStage.build_id == build.id, BuildStage.name == HIL_STAGE)
    ):
        raise ReportRejected(409, f"build already has a {HIL_STAGE} stage")

    summary = summarize(report.stages)
    index = classify.exit_code_index(summary.exit_code) if summary.status is StageStatus.failed else None
    stage = {
        "status": summary.status,
        "exit_code": summary.exit_code,
        "started_at": summary.started_at,
        "finished_at": summary.finished_at,
        "log_key": None,
        "log_index": index.to_json() if index is not None else None,
    }
    if existing is not None:
        session.add(
            AuditLog(
                action="linked_report.replaced",
                target_type="build",
                target_id=build.id,
                details={
                    "profile": report.profile,
                    "run_id": claims.run_id,
                    "from_attempt": existing.gh_run_attempt,
                    "to_attempt": claims.run_attempt,
                },
            )
        )
        existing.payload, existing.digest, existing.claims = payload, digest, claims.as_dict()
        existing.gh_run_attempt = claims.run_attempt
        existing.schema_version = report.schema_
        existing.received_at = func.now()
        await session.execute(
            update(BuildStage)
            .where(BuildStage.build_id == build.id, BuildStage.name == existing.stage)
            .values(**stage)
        )
    else:
        last = await session.scalar(
            select(func.max(BuildStage.position)).where(BuildStage.build_id == build.id)
        )
        session.add(
            BuildStage(
                build_id=build.id,
                name=HIL_STAGE,
                position=0 if last is None else last + 1,
                required=False,
                **stage,
            )
        )
        session.add(
            LinkedReport(
                build_id=build.id,
                profile=report.profile,
                stage=HIL_STAGE,
                schema_version=report.schema_,
                payload=payload,
                digest=digest,
                claims=claims.as_dict(),
                gh_run_id=claims.run_id,
                gh_run_attempt=claims.run_attempt,
            )
        )
    await session.flush()
    log.info(
        "linked report accepted",
        extra={
            "build_id": build.id,
            "profile": report.profile,
            "stage_status": summary.status.value,
            "run_id": claims.run_id,
            "attempt": claims.run_attempt,
        },
    )
    return Accepted(build.id, duplicate=False)
