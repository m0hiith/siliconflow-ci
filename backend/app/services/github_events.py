"""Apply GitHub workflow_run / workflow_job webhooks to builds.

One build is one workflow run *attempt*: re-running a run (attempt 2, 3, ...)
creates a new build row, so every attempt keeps its own history.

GitHub does not guarantee delivery order. Every handler is idempotent and
moves status only through services.transitions, whose guarded UPDATE ignores
events that arrive too late to matter.

The webhook never decides passed/failed on its own: a "success" or "failure"
conclusion is stored on the build, and services.outcome settles it with the
run report, or errors the build if no report arrives in time.
"""
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    TERMINAL_BUILD_STATUSES,
    Build,
    BuildStatus,
    BuildTrigger,
    Repository,
    WebhookEvent,
)
from app.services import outcome
from app.services.builds import create_build
from app.services.transitions import transition

log = logging.getLogger(__name__)

# Manual builds are dispatched with run-name "sfci <build_id>" (see
# .github/workflows/firmware-ci.yml); GitHub reports it as display_title.
RUN_NAME_RE = re.compile(r"^sfci (bld_[0-9A-HJKMNP-TV-Z]{26})$")

TRIGGERS = {"push": BuildTrigger.push, "pull_request": BuildTrigger.pull_request}

# Conclusions the webhook settles by itself. success/failure wait for the run
# report, and action_required waits for someone to approve the run (it then
# continues as the same run attempt). Any other conclusion, including ones
# GitHub adds later, errors the build: nothing says whether the firmware built.
CONCLUSION_STATUS = {
    "cancelled": BuildStatus.cancelled,
    "timed_out": BuildStatus.timed_out,
    "startup_failure": BuildStatus.errored,
    # The firmware jobs never ran: the run was skipped, or superseded.
    "skipped": BuildStatus.cancelled,
    "stale": BuildStatus.cancelled,
    # GitHub's "no verdict".
    "neutral": BuildStatus.errored,
}
AWAITING_APPROVAL = "action_required"

# A re-run attempt describes the same commit and trigger as the attempt before it.
INHERITED_FIELDS = (
    "project_id",
    "repository_id",
    "trigger",
    "profile",
    "ref",
    "branch",
    "commit_sha",
    "commit_message",
    "author_login",
    "pr_number",
    "triggered_by_user_id",
)


class RetryLater(Exception):
    """The event cannot be applied yet (e.g. it overtook the event that creates
    its build). The worker retries it with backoff."""


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def handle(session: AsyncSession, event: str, payload: dict[str, Any], settings: Settings) -> None:
    if event == "workflow_run":
        await handle_workflow_run(session, payload, settings)
    elif event == "workflow_job":
        await handle_workflow_job(session, payload)


async def handle_workflow_run(
    session: AsyncSession, payload: dict[str, Any], settings: Settings
) -> None:
    action = payload["action"]
    if action not in ("requested", "in_progress", "completed"):
        return
    run = payload["workflow_run"]

    repo = await find_repository(session, payload["repository"])
    if repo is None:
        log.info("workflow_run for unknown repository ignored", extra={"run_id": run["id"]})
        return

    build = await find_or_create_build(session, run, repo, settings)
    if build is None:
        return
    if action != "completed":
        await clear_awaiting_approval(session, build)
        return
    await apply_conclusion(session, build, run, settings)


async def clear_awaiting_approval(session: AsyncSession, build: Build) -> None:
    """An approved run starts again under the same attempt; its earlier
    action_required conclusion no longer describes it."""
    if build.gh_conclusion == AWAITING_APPROVAL:
        build.gh_conclusion = None
        await session.flush()


async def apply_conclusion(
    session: AsyncSession, build: Build, run: dict[str, Any], settings: Settings
) -> None:
    """Apply a completed run's conclusion to its build. Also used by the
    reconciler, with the run fetched from the API. The caller holds the run's
    advisory lock."""
    if build.status in TERMINAL_BUILD_STATUSES:
        # Finished builds keep the conclusion that finished them (or none: a
        # manual build errored before its run was bound).
        return
    conclusion = run.get("conclusion")
    build.gh_conclusion = conclusion
    await session.flush()
    if conclusion is None or conclusion == AWAITING_APPROVAL:
        return
    if conclusion in outcome.DECIDED_BY_REPORT:
        if not await outcome.settle(session, build.id, parse_ts(run["updated_at"])):
            await outcome.schedule_deadline(session, build.id, settings)
        return
    target = CONCLUSION_STATUS.get(conclusion)
    if target is None:
        log.warning("unknown workflow run conclusion", extra={"build_id": build.id, "conclusion": conclusion})
        target = BuildStatus.errored
    if target is BuildStatus.timed_out:
        # A timed-out run certainly started, even if its workflow_job
        # in_progress event has not arrived yet; record that first.
        await transition(
            session,
            build.id,
            BuildStatus.running,
            parse_ts(run.get("run_started_at") or run["created_at"]),
        )
    await transition(
        session,
        build.id,
        target,
        parse_ts(run["updated_at"]),
        reason=f"workflow run concluded: {conclusion}",
    )


async def handle_workflow_job(session: AsyncSession, payload: dict[str, Any]) -> None:
    if payload["action"] != "in_progress":
        return
    job = payload["workflow_job"]
    run_id, attempt = job["run_id"], job["run_attempt"]
    build_id = await session.scalar(
        select(Build.id).where(Build.gh_run_id == run_id, Build.gh_run_attempt == attempt)
    )
    if build_id is None:
        if await run_is_untracked(session, run_id, attempt):
            return
        raise RetryLater(f"no build yet for run {run_id} attempt {attempt}")
    await transition(session, build_id, BuildStatus.running, parse_ts(job["started_at"]))


async def run_is_untracked(session: AsyncSession, run_id: int, attempt: int) -> bool:
    """Whether a workflow_run event of this run attempt was processed and
    created no build: the run is one SiliconFlow does not track (another
    workflow, repository or event), so its job events need no retries."""
    run = WebhookEvent.payload["workflow_run"]
    return bool(
        await session.scalar(
            select(
                exists().where(
                    WebhookEvent.event == "workflow_run",
                    WebhookEvent.processed_at.is_not(None),
                    run["id"].as_integer() == run_id,
                    run["run_attempt"].as_integer() == attempt,
                )
            )
        )
    )


async def find_repository(session: AsyncSession, repo: dict[str, Any]) -> Repository | None:
    """Match by GitHub's numeric id, which survives renames; fall back to the
    name for repositories registered before their id was known, and record it."""
    found = await session.scalar(select(Repository).where(Repository.gh_repo_id == repo["id"]))
    if found is not None:
        return found
    found = await session.scalar(
        select(Repository).where(
            Repository.full_name == repo["full_name"], Repository.gh_repo_id.is_(None)
        )
    )
    if found is not None:
        found.gh_repo_id = repo["id"]
    return found


async def find_or_create_build(
    session: AsyncSession, run: dict[str, Any], repo: Repository, settings: Settings
) -> Build | None:
    """The build for this run attempt, creating or binding it if needed.

    Lookup order:
      1. a build already bound to (run id, attempt);
      2. a manual build named in the run name "sfci <build_id>", not yet bound;
      3. a new build for a re-run, copied from the run's latest earlier attempt;
      4. a new build for a push or pull_request run of a firmware workflow.
    Returns None for runs SiliconFlow does not track.
    """
    run_id, attempt = run["id"], run["run_attempt"]
    # Events for one run are serialised so two workers cannot both create its build.
    await session.execute(select(func.pg_advisory_xact_lock(run_id)))

    build = await session.scalar(
        select(Build).where(Build.gh_run_id == run_id, Build.gh_run_attempt == attempt)
    )
    if build is not None:
        return build

    m = RUN_NAME_RE.match(run.get("display_title") or "")
    if m:
        named = await session.scalar(
            select(Build).where(Build.id == m[1]).with_for_update()
        )
        if named is not None and named.gh_run_id is None and named.repository_id == repo.id:
            named.gh_run_id, named.gh_run_attempt = run_id, attempt
            # A manual build learns its commit from the run (the run report is matched against it).
            named.commit_sha = named.commit_sha or run.get("head_sha")
            named.commit_message = named.commit_message or (run.get("head_commit") or {}).get("message")
            await session.flush()
            log.info("build bound to run", extra={"build_id": named.id, "run_id": run_id})
            return named

    workflow_path = run.get("path") or ""
    if settings.required_stages_for(workflow_path) is None:
        return None

    previous = await session.scalar(
        select(Build)
        .where(Build.gh_run_id == run_id, Build.gh_run_attempt < attempt)
        .order_by(Build.gh_run_attempt.desc())
        .limit(1)
    )
    if previous is not None:
        fields = {name: getattr(previous, name) for name in INHERITED_FIELDS}
    elif (trigger := TRIGGERS.get(run.get("event"))) is not None:
        fields = fields_from_run(run, repo, trigger, workflow_path)
    else:
        return None

    build = await create_build(
        session,
        **fields,
        gh_run_id=run_id,
        gh_run_attempt=attempt,
        queued_at=parse_ts(run.get("run_started_at") or run["created_at"]),
    )
    log.info(
        "build created from workflow run",
        extra={"build_id": build.id, "run_id": run_id, "attempt": attempt},
    )
    return build


def workflow_profile(workflow_path: str) -> str:
    """A build's profile until its run report names the job that ran (services.run_reports)."""
    return workflow_path.rsplit("/", 1)[-1].removesuffix(".yml").removesuffix(".yaml")


def fields_from_run(
    run: dict[str, Any], repo: Repository, trigger: BuildTrigger, workflow_path: str
) -> dict[str, Any]:
    prs = run.get("pull_requests") or []
    # Fork PRs arrive with an empty pull_requests list; they keep the head branch ref.
    pr_number = prs[0]["number"] if trigger is BuildTrigger.pull_request and prs else None
    ref = f"refs/pull/{pr_number}/merge" if pr_number else f"refs/heads/{run['head_branch']}"
    actor = run.get("triggering_actor") or run.get("actor") or {}
    return {
        "project_id": repo.project_id,
        "repository_id": repo.id,
        "trigger": trigger,
        "profile": workflow_profile(workflow_path),
        "ref": ref,
        "branch": run.get("head_branch"),
        "commit_sha": run.get("head_sha"),
        "commit_message": (run.get("head_commit") or {}).get("message"),
        "author_login": actor.get("login"),
        "pr_number": pr_number,
    }
