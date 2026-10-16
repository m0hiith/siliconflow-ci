"""services.reconciler: builds whose webhooks were lost still converge, using
the run as the GitHub API (respx-mocked) reports it."""
import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import delete, func, select, update

from app import worker
from app.models import (
    TERMINAL_BUILD_STATUSES,
    Build,
    BuildStatus,
    BuildTrigger,
    Job,
    JobStatus,
    Repository,
    WebhookEvent,
)
from app.services import reconciler
from app.services.github import GitHubApp
from app.services.transitions import transition
from tests import oidc, reports
from tests.conftest import GITHUB_API
from tests.factories import make_build, make_project
from tests.github import deliver, drain, make_retries_ready, workflow_job, workflow_run
from tests.reports import RUN_ID, post

S = BuildStatus
INSTALLATION = 42
NOW = datetime.now(UTC).replace(microsecond=0)
# Well past build_timeout_s + reconcile_grace_s (20 minutes by default).
QUEUED, STARTED, FINISHED = (NOW - timedelta(minutes=m) for m in (40, 39, 35))
RUN_URL = rf"{GITHUB_API}/repos/acme/fw/actions/runs/(?P<run_id>\d+)/attempts/(?P<attempt>\d+)$"


def iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


class FakeGitHub:
    """The runs API: GET .../runs/{id}/attempts/{n} answers from `runs`."""

    def __init__(self, router: respx.Router) -> None:
        self.runs: dict[tuple[int, int], dict | int] = {}
        router.post(f"{GITHUB_API}/app/installations/{INSTALLATION}/access_tokens").respond(
            201,
            json={"token": "ghs_installation", "expires_at": iso(NOW + timedelta(hours=2))},
        )
        self.installation = router.get(f"{GITHUB_API}/repos/acme/fw/installation").respond(
            200, json={"id": INSTALLATION}
        )
        self.lookups = router.get(url__regex=RUN_URL).mock(side_effect=self._run)

    def _run(self, request: httpx.Request, run_id: str, attempt: str) -> httpx.Response:
        run = self.runs.get((int(run_id), int(attempt)), 404)
        if isinstance(run, int):
            return httpx.Response(run, json={"message": "Not Found"})
        return httpx.Response(200, json=run)

    def set_run(self, repo, *, run_id=RUN_ID, status: str, conclusion: str | None = None) -> None:
        """The run as GET /actions/runs/{id}/attempts/{n} returns it: the same
        object a workflow_run webhook carries."""
        action = {"in_progress": "in_progress", "completed": "completed"}[status]
        run = workflow_run(
            action, repo, run_id=run_id, conclusion=conclusion,
            started_at=iso(QUEUED), updated_at=iso(FINISHED),
        )["workflow_run"]
        self.runs[(run_id, 1)] = run


@pytest.fixture(scope="module")
def key():
    return oidc.new_key()


@pytest.fixture
def mock(key):
    with respx.mock(assert_all_called=False) as router:
        router.get(oidc.JWKS_URL).respond(json=oidc.jwks((key, oidc.KID)))
        yield router


@pytest.fixture
def gh(mock) -> FakeGitHub:
    return FakeGitHub(mock)


@pytest.fixture
async def github(settings):
    client = GitHubApp(settings)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def project(session):
    project = await make_project(session, "fw")
    repo = await session.get_one(Repository, project.repositories[0].id)
    repo.gh_installation_id = INSTALLATION
    await session.commit()
    return project


@pytest.fixture
def repo(project) -> Repository:
    return project.repositories[0]


async def _only_build(session) -> Build:
    session.expunge_all()
    return (await session.scalars(select(Build))).one()


async def _count(session, model, *where) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(*where))


def _requested(repo, run_id=RUN_ID):
    return ("workflow_run", workflow_run("requested", repo, run_id=run_id, started_at=iso(QUEUED)))


def _job(repo, run_id=RUN_ID):
    return ("workflow_job", workflow_job(repo, run_id=run_id, started_at=iso(STARTED)))


def _completed(repo, conclusion, run_id=RUN_ID):
    return (
        "workflow_run",
        workflow_run(
            "completed", repo, run_id=run_id, conclusion=conclusion,
            started_at=iso(QUEUED), updated_at=iso(FINISHED),
        ),
    )


async def _webhooks(client, sessionmaker, settings, *events):
    for event, payload in events:
        assert (await deliver(client, event, payload)).status_code == 202
    await drain(sessionmaker, settings)


async def _lose(client, session, event):
    """GitHub delivers the webhook, then it is deleted before the worker runs it."""
    delivery_id = f"lost-{event[1]['action']}"
    assert (await deliver(client, *event, delivery_id=delivery_id)).status_code == 202
    await session.execute(delete(Job).where(Job.payload["delivery_id"].astext == delivery_id))
    await session.execute(delete(WebhookEvent).where(WebhookEvent.delivery_id == delivery_id))
    await session.commit()
    assert await _count(session, WebhookEvent, WebhookEvent.delivery_id == delivery_id) == 0


def _corrections(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "build corrected from the GitHub API"]


# ------------------------------------------------------- lost webhooks


async def test_lost_completed_webhook_still_converges_to_passed(
    client, session, sessionmaker, settings, github, gh, key, repo, caplog
):
    await _webhooks(client, sessionmaker, settings, _requested(repo), _job(repo))
    body = reports.report(artifact_name=None, manifest=None, junit=[])
    assert (await post(client, body, reports.token(key))).status_code == 202
    await _lose(client, session, _completed(repo, "success"))
    await drain(sessionmaker, settings)
    assert (await _only_build(session)).status is S.running  # stuck without the reconciler

    gh.set_run(repo, status="completed", conclusion="success")
    with caplog.at_level(logging.INFO, logger=reconciler.__name__):
        assert await reconciler.reconcile(sessionmaker, settings, github) == 1

    build = await _only_build(session)
    assert (build.status, build.gh_conclusion) == (S.passed, "success")
    assert build.status_reason == "workflow run succeeded and every required stage passed"
    assert build.finished_at == FINISHED
    (record,) = _corrections(caplog)
    assert record.levelno == logging.WARNING
    assert (record.build_id, record.run_id, record.attempt) == (build.id, RUN_ID, 1)
    assert (record.from_status, record.to_status) == ("running", "passed")
    assert (record.gh_status, record.gh_conclusion) == ("completed", "success")

    # GitHub redelivering the lost webhook later changes nothing.
    await _webhooks(client, sessionmaker, settings, _completed(repo, "success"))
    build = await _only_build(session)
    assert (build.status, build.finished_at) == (S.passed, FINISHED)
    # A passed build is never looked at again.
    assert await reconciler.reconcile(sessionmaker, settings, github) == 0
    assert gh.lookups.call_count == 1


@pytest.mark.parametrize(
    ("conclusion", "status"),
    [("cancelled", S.cancelled), ("timed_out", S.timed_out), ("startup_failure", S.errored)],
)
async def test_lost_job_and_completed_webhooks_converge(
    client, session, sessionmaker, settings, github, gh, repo, conclusion, status
):
    await _webhooks(client, sessionmaker, settings, _requested(repo))
    await _lose(client, session, _job(repo))
    await _lose(client, session, _completed(repo, conclusion))
    assert (await _only_build(session)).status is S.queued

    gh.set_run(repo, status="completed", conclusion=conclusion)
    assert await reconciler.reconcile(sessionmaker, settings, github) == 1

    build = await _only_build(session)
    assert (build.status, build.gh_conclusion) == (status, conclusion)
    assert build.status_reason == f"workflow run concluded: {conclusion}"
    assert build.finished_at == FINISHED
    # Only a timed-out run is known to have started.
    assert build.started_at == (QUEUED if status is S.timed_out else None)


async def test_lost_success_without_a_report_errors_at_the_report_deadline(
    client, session, sessionmaker, settings, github, gh, repo, caplog
):
    await _webhooks(client, sessionmaker, settings, _requested(repo), _job(repo))
    await _lose(client, session, _completed(repo, "success"))

    gh.set_run(repo, status="completed", conclusion="success")
    with caplog.at_level(logging.INFO, logger=reconciler.__name__):
        assert await reconciler.reconcile(sessionmaker, settings, github) == 1
    build = await _only_build(session)
    # Recorded, but success still waits for the run report.
    assert (build.status, build.gh_conclusion) == (S.running, "success")
    (record,) = _corrections(caplog)
    assert (record.from_status, record.to_status, record.gh_conclusion) == ("running", "running", "success")
    assert await _count(session, Job, Job.kind == "report_deadline", Job.status == JobStatus.queued) == 1

    # The deadline job owns the build now; later passes leave it alone.
    assert await reconciler.reconcile(sessionmaker, settings, github) == 0
    assert gh.lookups.call_count == 1

    await make_retries_ready(sessionmaker)
    await drain(sessionmaker, settings)
    build = await _only_build(session)
    assert build.status is S.errored
    assert build.status_reason.startswith("workflow run concluded success but no run report arrived")


async def test_lost_job_webhook_moves_a_queued_build_to_running(
    client, session, sessionmaker, settings, github, gh, repo
):
    await _webhooks(client, sessionmaker, settings, _requested(repo))
    await _lose(client, session, _job(repo))

    gh.set_run(repo, status="in_progress")
    assert await reconciler.reconcile(sessionmaker, settings, github) == 1
    build = await _only_build(session)
    assert (build.status, build.started_at, build.gh_conclusion) == (S.running, QUEUED, None)


# ------------------------------------------------------- stuck builds
# Builds that no webhook would ever move again still finish.


@pytest.mark.parametrize(
    ("conclusion", "status"),
    [("skipped", S.cancelled), ("stale", S.cancelled), ("neutral", S.errored), ("from_the_future", S.errored)],
)
async def test_every_completed_run_finishes_its_build(
    client, session, sessionmaker, settings, github, gh, repo, conclusion, status
):
    await _webhooks(client, sessionmaker, settings, _requested(repo), _job(repo), _completed(repo, conclusion))

    build = await _only_build(session)
    assert (build.status, build.gh_conclusion) == (status, conclusion)
    assert build.status in TERMINAL_BUILD_STATUSES
    assert build.status_reason == f"workflow run concluded: {conclusion}"
    assert await _count(session, Job, Job.kind == "report_deadline") == 0


async def test_a_run_awaiting_approval_waits_and_is_still_reconciled(
    client, session, sessionmaker, settings, github, gh, repo
):
    await _webhooks(client, sessionmaker, settings, _requested(repo), _completed(repo, "action_required"))
    build = await _only_build(session)
    assert (build.status, build.gh_conclusion) == (S.queued, "action_required")
    assert await _count(session, Job, Job.kind == "report_deadline") == 0

    # Approved, then its webhooks were lost: the reconciler still looks it up.
    gh.set_run(repo, status="completed", conclusion="cancelled")
    assert await reconciler.reconcile(sessionmaker, settings, github) == 1
    build = await _only_build(session)
    assert (build.status, build.gh_conclusion) == (S.cancelled, "cancelled")


async def test_a_lost_deadline_job_is_done_by_the_reconciler(
    client, session, sessionmaker, settings, github, gh, repo, caplog
):
    await _webhooks(client, sessionmaker, settings, _requested(repo), _job(repo), _completed(repo, "success"))
    # The deadline job failed for good, and nothing has touched the build since.
    await session.execute(
        update(Job).where(Job.kind == "report_deadline").values(status=JobStatus.failed)
    )
    await session.execute(update(Build).values(updated_at=NOW - timedelta(minutes=30)))
    await session.commit()

    with caplog.at_level(logging.INFO, logger=reconciler.__name__):
        assert await reconciler.reconcile(sessionmaker, settings, github) == 1
    build = await _only_build(session)
    assert build.status is S.errored
    assert build.status_reason.startswith("workflow run concluded success but no run report arrived")
    assert gh.lookups.call_count == 0  # GitHub has nothing more to say about a concluded run
    assert any(r.getMessage() == "build settled after its report deadline job was lost" for r in caplog.records)


async def test_a_build_waiting_for_its_deadline_is_left_to_the_deadline_job(
    client, session, sessionmaker, settings, github, gh, repo
):
    await _webhooks(client, sessionmaker, settings, _requested(repo), _job(repo), _completed(repo, "success"))
    assert await reconciler.reconcile(sessionmaker, settings, github) == 0
    assert (await _only_build(session)).status is S.running


async def test_a_manual_build_whose_run_never_appears_does_not_stay_queued(
    client, session, sessionmaker, settings, github, gh, project, repo
):
    # The API committed the build and then stopped before dispatching, or
    # GitHub accepted the dispatch and never started a run.
    manual = await make_build(session, project, trigger=BuildTrigger.manual, queued_at=QUEUED)
    recent = await make_build(session, project, trigger=BuildTrigger.manual, queued_at=NOW - timedelta(minutes=5))
    await session.commit()

    assert await reconciler.reconcile(sessionmaker, settings, github) == 1
    assert gh.lookups.call_count == 0
    session.expunge_all()
    build = await session.get_one(Build, manual.id)
    assert build.status is S.errored
    assert build.status_reason == "no workflow run started for this manual build within 20 minutes"
    assert (await session.get_one(Build, recent.id)).status is S.queued

    # A run that turns up after all is bound, but cannot reopen the build or
    # rewrite how it finished.
    title = f"sfci {manual.id}"
    await _webhooks(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID, event="workflow_dispatch", display_title=title)),
        ("workflow_run", workflow_run(
            "completed", repo, run_id=RUN_ID, event="workflow_dispatch", display_title=title, conclusion="success"
        )),
    )
    session.expunge_all()
    build = await session.get_one(Build, manual.id)
    assert (build.gh_run_id, build.status, build.gh_conclusion) == (RUN_ID, S.errored, None)
    assert await _count(session, Job, Job.kind == "report_deadline") == 0


# -------------------------------------------------- what gets checked


async def _run_build(session, project, *, run_id=RUN_ID, queued_at=QUEUED, started_at=STARTED, status=S.running):
    build = await make_build(
        session, project, gh_run_id=run_id, gh_run_attempt=1, queued_at=queued_at
    )
    if status is not S.queued:
        await transition(session, build.id, S.running, started_at)
    if status not in (S.queued, S.running):
        await transition(session, build.id, status, started_at + timedelta(minutes=1))
    await session.commit()
    return build


async def test_builds_inside_their_timeout_are_not_fetched(session, sessionmaker, settings, github, gh, project):
    # Queued long ago, but it started only 10 minutes ago: the job timeout runs from the start.
    await _run_build(session, project, started_at=NOW - timedelta(minutes=10))
    await _run_build(session, project, run_id=RUN_ID + 1, queued_at=NOW - timedelta(minutes=19), status=S.queued)
    assert await reconciler.reconcile(sessionmaker, settings, github) == 0
    assert gh.lookups.call_count == 0


async def test_finished_and_unbound_builds_are_not_fetched(session, sessionmaker, settings, github, gh, project):
    await _run_build(session, project, status=S.cancelled)
    # A manual build whose run was never bound has no run to look up; it is
    # errored without one (test_a_manual_build_whose_run_never_appears_...).
    await make_build(session, project, trigger=BuildTrigger.manual, queued_at=QUEUED)
    await session.commit()
    assert await reconciler.reconcile(sessionmaker, settings, github) == 1
    assert gh.lookups.call_count == 0


async def test_a_run_still_in_progress_is_left_alone_and_checked_again(
    session, sessionmaker, settings, github, gh, project, repo, caplog
):
    build = await _run_build(session, project)
    gh.set_run(repo, status="in_progress")
    with caplog.at_level(logging.INFO, logger=reconciler.__name__):
        assert await reconciler.reconcile(sessionmaker, settings, github) == 0
        assert await reconciler.reconcile(sessionmaker, settings, github) == 0
    assert gh.lookups.call_count == 2
    assert _corrections(caplog) == []
    assert (await _only_build(session)).updated_at == build.updated_at


async def test_one_failing_lookup_does_not_stop_the_pass(
    session, sessionmaker, settings, github, gh, project, repo, caplog
):
    gone = await _run_build(session, project, run_id=RUN_ID + 1)
    stuck = await _run_build(session, project)
    gh.runs[(RUN_ID + 1, 1)] = 404
    gh.set_run(repo, status="completed", conclusion="cancelled")

    beats = []
    with caplog.at_level(logging.INFO, logger=reconciler.__name__):
        assert await reconciler.reconcile(sessionmaker, settings, github, lambda: beats.append(1)) == 1
    assert len(beats) == 2

    session.expunge_all()
    assert (await session.get_one(Build, gone.id)).status is S.running
    assert (await session.get_one(Build, stuck.id)).status is S.cancelled
    (failed,) = [r for r in caplog.records if r.getMessage() == "build reconcile failed"]
    assert failed.build_id == gone.id
    assert failed.error == "GitHubError: run lookup: GitHub answered 404 (Not Found)"


async def test_missing_installation_id_is_looked_up_and_stored(
    session, sessionmaker, settings, github, gh, project, repo
):
    stored = await session.get_one(Repository, repo.id)
    stored.gh_installation_id = None
    await session.commit()
    await _run_build(session, project)
    await _run_build(session, project, run_id=RUN_ID + 1)
    gh.set_run(repo, status="in_progress")
    gh.set_run(repo, run_id=RUN_ID + 1, status="in_progress")

    await reconciler.reconcile(sessionmaker, settings, github)
    assert gh.installation.call_count == 1  # once per repository per pass
    session.expunge_all()
    assert (await session.get_one(Repository, repo.id)).gh_installation_id == INSTALLATION


# --------------------------------------------------------- worker loop


async def _run_worker(settings, monkeypatch, tmp_path, *, until_passes=None, for_s=None) -> list[float]:
    """Run the real worker loop with reconcile() replaced by a recorder."""
    calls: list[float] = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def fake_reconcile(sessionmaker, settings, github, heartbeat):
        calls.append(loop.time())
        if len(calls) == until_passes:
            stop.set()

    monkeypatch.setattr(worker.reconciler, "reconcile", fake_reconcile)
    if for_s is not None:
        loop.call_later(for_s, stop.set)
    s = settings.model_copy(
        update={"worker_poll_interval_s": 0.01, "worker_heartbeat_path": str(tmp_path / "heartbeat")}
    )
    await asyncio.wait_for(worker.run(s, stop), timeout=5)
    return calls


async def test_worker_reconciles_at_startup_and_then_every_interval(settings, engine, monkeypatch, tmp_path):
    s = settings.model_copy(update={"reconcile_interval_s": 0.1})
    first, second, third = await _run_worker(s, monkeypatch, tmp_path, until_passes=3)
    assert second - first >= 0.1 and third - second >= 0.1


async def test_worker_without_a_github_app_does_not_reconcile(settings, engine, monkeypatch, tmp_path):
    s = settings.model_copy(update={"github_app_id": None, "reconcile_interval_s": 0.01})
    assert await _run_worker(s, monkeypatch, tmp_path, for_s=0.2) == []
