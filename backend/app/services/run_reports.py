"""Accept run reports: the facts a CI job states about itself.

The OIDC token, not the report body, says which run a report is from. The run
must belong to a build SiliconFlow tracks, from the same repository, the
firmware workflow, and the build's commit. A report for a finished build is
refused, since terminal states never change.

Stages are stored as reported, except that the backend's required stages for
the token's workflow (settings.required_stages_for) decide which stages must
pass: a report can mark more stages required but cannot drop one, and a
required stage it does not mention is stored as skipped.
services.outcome decides the build's status; artifacts and JUnit files are
fetched and verified later by a worker job (services.artifacts). A reported
ELF size is stored in build_sizes for display and comparison only.

The report's github.job becomes the build's profile. Until a report arrives a
build's profile is its workflow's name; the job is what ran, produced the
artifacts and is gated, and it is what gates.yml's `profile` refers to. The
job name is the runner's claim (OIDC tokens do not carry it), but it comes
from the workflow file at the build's commit, the same commit gates.yml is
read at, so it lets nobody past a gate they could not edit anyway.
"""
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    TERMINAL_BUILD_STATUSES,
    Build,
    BuildSize,
    BuildStage,
    BuildStatus,
    BuildTrigger,
    Job,
    Repository,
    RunReport,
    StageStatus,
)
from app.schemas import RunReportIn
from app.services import outcome
from app.services.artifacts import INGEST_JOB_KIND
from app.services.oidc import RunClaims
from app.services.transitions import transition

log = logging.getLogger(__name__)

# A job id as GitHub allows it, kept to the characters a profile filter can carry.
PROFILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class ReportRejected(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class Accepted:
    build_id: str
    duplicate: bool


def payload_of(report: RunReportIn) -> dict:
    return report.model_dump(mode="json", by_alias=True)


def digest_of(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def check_identity(claims: RunClaims, report: RunReportIn, settings: Settings) -> tuple[str, ...]:
    """The body must describe the run the token was issued to, and the token
    must come from a firmware workflow. Returns that workflow's required stages."""
    gh = report.github
    if (
        gh.repository.lower() != claims.repository.lower()
        or (gh.run_id, gh.run_attempt, gh.sha) != (claims.run_id, claims.run_attempt, claims.sha)
    ):
        raise ReportRejected(403, "report does not describe the run its token was issued to")
    workflow = claims.workflow_ref.split("@", 1)[0]
    prefix = f"{claims.repository}/".lower()
    required = None
    if workflow.lower().startswith(prefix):
        required = settings.required_stages_for(workflow[len(prefix):])
    if required is None:
        raise ReportRejected(403, "token is not from a firmware workflow")
    return required


def check_build(build: Build, repo: Repository, claims: RunClaims) -> None:
    if repo.full_name.lower() != claims.repository.lower():
        raise ReportRejected(403, "token repository does not match the build")
    if repo.gh_repo_id is not None and claims.repository_id is not None:
        if str(repo.gh_repo_id) != claims.repository_id:
            raise ReportRejected(403, "token repository id does not match the build")
    if build.trigger is BuildTrigger.pull_request:
        # Pull request jobs run on GitHub's merge commit, which is the token's
        # sha; the build records the PR head. The merge ref ties them together.
        if claims.ref != build.ref:
            raise ReportRejected(403, "token ref does not match the build")
    elif build.commit_sha is None or claims.sha != build.commit_sha:
        raise ReportRejected(403, "token commit does not match the build")


def stage_rows(build_id: str, report: RunReportIn, required: tuple[str, ...]) -> list[dict]:
    rows = [
        {
            "build_id": build_id,
            "name": s.name,
            "position": i,
            "required": s.required or s.name in required,
            "status": StageStatus(s.status),
            "exit_code": s.exit_code,
            "started_at": s.started_at,
            "finished_at": s.finished_at,
        }
        for i, s in enumerate(report.stages)
    ]
    reported = {s.name for s in report.stages}
    for name in required:
        if name not in reported:
            rows.append(
                {
                    "build_id": build_id,
                    "name": name,
                    "position": len(rows),
                    "required": True,
                    "status": StageStatus.skipped,
                    "exit_code": None,
                    "started_at": None,
                    "finished_at": None,
                }
            )
    return rows


async def accept_report(
    session: AsyncSession, claims: RunClaims, report: RunReportIn, settings: Settings
) -> Accepted:
    """Store a verified report and settle the build if it can be. The caller commits."""
    required = check_identity(claims, report, settings)
    # Serialised with the run's webhooks, so the report and the "completed"
    # event cannot both miss each other (see services.outcome).
    await session.execute(select(func.pg_advisory_xact_lock(claims.run_id)))

    build = await session.scalar(
        select(Build).where(
            Build.gh_run_id == claims.run_id, Build.gh_run_attempt == claims.run_attempt
        )
    )
    if build is None:
        raise ReportRejected(404, "no build for this run yet")
    repo = await session.get_one(Repository, build.repository_id)
    check_build(build, repo, claims)

    payload = payload_of(report)
    digest = digest_of(payload)
    existing = await session.scalar(select(RunReport).where(RunReport.build_id == build.id))
    if existing is not None:
        if existing.digest == digest:
            return Accepted(build.id, duplicate=True)
        raise ReportRejected(409, "a different report was already recorded for this build")
    if build.status in TERMINAL_BUILD_STATUSES:
        raise ReportRejected(409, f"build is already {build.status.value}")

    if report.github.job and PROFILE_RE.fullmatch(report.github.job):
        build.profile = report.github.job

    # Fetch even without a manifest or JUnit: a build that failed to compile
    # has neither, and its stage logs are in the zip too.
    fetch = report.artifact_name is not None
    session.add(
        RunReport(
            build_id=build.id,
            schema_version=report.schema_,
            payload=payload,
            digest=digest,
            claims=claims.as_dict(),
            artifacts_state="pending" if fetch else "none",
        )
    )
    if (size := report.size) is not None:
        session.add(
            BuildSize(
                build_id=build.id,
                file_name=size.file,
                text_bytes=size.text_bytes,
                data_bytes=size.data_bytes,
                bss_bytes=size.bss_bytes,
                sections=[s.model_dump() for s in size.sections],
            )
        )
    if rows := stage_rows(build.id, report, required):
        await session.execute(insert(BuildStage), rows)
    if fetch:
        await session.execute(
            insert(Job).values(kind=INGEST_JOB_KIND, payload={"build_id": build.id})
        )

    now = datetime.now(UTC)
    if build.status is BuildStatus.queued:
        # A report proves the job ran, even if its workflow_job event is late.
        started = min((s.started_at for s in report.stages if s.started_at), default=now)
        await transition(session, build.id, BuildStatus.running, started)
    await session.flush()
    await outcome.settle(session, build.id, now)
    log.info(
        "run report accepted",
        extra={"build_id": build.id, "run_id": claims.run_id, "attempt": claims.run_attempt},
    )
    return Accepted(build.id, duplicate=False)
