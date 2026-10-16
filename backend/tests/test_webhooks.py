"""POST /api/v1/webhooks/github and the worker that applies webhook jobs."""
import itertools
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.config import Settings
from app.main import create_app
from app.models import Build, BuildStatus, BuildTrigger, Job, JobStatus, Repository, WebhookEvent
from app.services.webhooks import signature_for
from app.worker import backoff
from tests.conftest import WEBHOOK_SECRET
from tests.factories import make_build, make_project
from tests.github import deliver, drain, make_retries_ready, workflow_job, workflow_run

S = BuildStatus
RUN_ID = 30433642


async def _count(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def _builds(session) -> list[Build]:
    session.expunge_all()
    return list((await session.scalars(select(Build).order_by(Build.number))).all())


@pytest.fixture
async def project(session):
    project = await make_project(session, "fw")
    await session.commit()
    return project


@pytest.fixture
def repo(project):
    return project.repositories[0]


# ------------------------------------------------------------- signature


async def _post(client, body: bytes, **headers):
    return await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={"x-github-event": "workflow_run", "x-github-delivery": "d-1", **headers},
    )


@pytest.mark.parametrize(
    "signature",
    [
        None,  # missing
        "",
        "sha256=" + "0" * 64,  # well-formed, wrong
        signature_for(b"wrong-secret", b'{"action":"requested"}'),
        signature_for(WEBHOOK_SECRET.encode(), b'{"action": "requested"}'),  # re-serialised body
        "sha1=" + "0" * 40,
    ],
)
async def test_bad_signature_is_401_and_stores_nothing(client, session, signature):
    headers = {} if signature is None else {"x-hub-signature-256": signature}
    r = await _post(client, b'{"action":"requested"}', **headers)
    assert r.status_code == 401
    assert await _count(session, WebhookEvent) == 0
    assert await _count(session, Job) == 0


async def test_signature_checked_before_body_is_parsed(client):
    r = await _post(client, b"not json", **{"x-hub-signature-256": "sha256=" + "0" * 64})
    assert r.status_code == 401  # not 400: the body is never parsed

    body = b"not json"
    r = await _post(client, body, **{"x-hub-signature-256": signature_for(WEBHOOK_SECRET.encode(), body)})
    assert r.status_code == 400


async def test_missing_delivery_header_is_400(client):
    body = b"{}"
    r = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "x-github-event": "workflow_run",
            "x-hub-signature-256": signature_for(WEBHOOK_SECRET.encode(), body),
        },
    )
    assert r.status_code == 400


async def test_no_configured_secret_rejects_everything(settings, engine):
    app = create_app(settings.model_copy(update={"github_webhook_secret": None}))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            body = b"{}"
            r = await _post(c, body, **{"x-hub-signature-256": signature_for(b"", body)})
            assert r.status_code == 503
    finally:
        await app.state.github.aclose()
        await app.state.oidc.aclose()
        await app.state.engine.dispose()


def test_empty_secret_from_env_counts_as_unset(database_url):
    assert Settings(database_url=database_url, github_webhook_secret="").github_webhook_secret is None


# ----------------------------------------------------- dedupe and enqueue


async def test_accepted_delivery_is_stored_and_queued(client, session, repo):
    r = await deliver(client, "workflow_run", workflow_run("requested", repo, run_id=RUN_ID), delivery_id="d-1")
    assert r.status_code == 202 and r.json() == {"status": "accepted"}

    event = await session.get_one(WebhookEvent, "d-1")
    assert (event.event, event.action, event.processed_at) == ("workflow_run", "requested", None)
    job = (await session.scalars(select(Job))).one()
    assert job.kind == "github_webhook" and job.payload == {"delivery_id": "d-1"}
    assert job.status is JobStatus.queued
    assert await _count(session, Build) == 0  # no build work on the request path


async def test_duplicate_delivery_is_200_and_does_no_work(client, session, sessionmaker, settings, repo):
    payload = workflow_run("requested", repo, run_id=RUN_ID)
    assert (await deliver(client, "workflow_run", payload, delivery_id="d-1")).status_code == 202
    await drain(sessionmaker, settings)

    r = await deliver(client, "workflow_run", payload, delivery_id="d-1")
    assert r.status_code == 200 and r.json() == {"status": "duplicate"}
    assert await _count(session, WebhookEvent) == 1
    assert await _count(session, Job) == 1
    assert await drain(sessionmaker, settings) == 0
    assert len(await _builds(session)) == 1


async def test_unhandled_event_is_recorded_without_a_job(client, session):
    r = await deliver(client, "ping", {"zen": "Keep it logically awesome."}, delivery_id="p-1")
    assert r.status_code == 202
    event = await session.get_one(WebhookEvent, "p-1")
    assert event.processed_at is not None
    assert await _count(session, Job) == 0


# -------------------------------------------------------- build lifecycle


async def _send(client, sessionmaker, settings, *events):
    for event, payload in events:
        assert (await deliver(client, event, payload)).status_code == 202
    await drain(sessionmaker, settings)


async def test_push_run_creates_build_and_job_marks_running(client, session, sessionmaker, settings, repo):
    await _send(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID)),
    )
    (build,) = await _builds(session)
    assert build.id.startswith("bld_") and build.number == 1
    assert (build.status, build.trigger) == (S.queued, BuildTrigger.push)
    assert (build.gh_run_id, build.gh_run_attempt) == (RUN_ID, 1)
    assert build.commit_sha == "3484a3fb816e0859fd6e1cea078d76385ff50625"
    assert (build.ref, build.branch) == ("refs/heads/master", "master")
    assert build.commit_message == "build(test): check workflow run event payload"
    assert build.author_login == "Codertocat"
    assert build.profile == "firmware-ci"
    assert build.queued_at == datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
    # The repository was matched by name and its GitHub id recorded.
    assert (await session.get_one(Repository, repo.id)).gh_repo_id == 300029405

    await _send(client, sessionmaker, settings, ("workflow_job", workflow_job(repo, run_id=RUN_ID)))
    (build,) = await _builds(session)
    assert build.status is S.running
    assert build.started_at == datetime(2026, 10, 14, 9, 1, tzinfo=timezone.utc)


async def test_pull_request_run_records_pr(client, session, sessionmaker, settings, repo):
    payload = workflow_run("requested", repo, run_id=RUN_ID, event="pull_request")
    payload["workflow_run"]["pull_requests"] = [{"number": 2, "id": 1, "url": "", "head": {}, "base": {}}]
    await _send(client, sessionmaker, settings, ("workflow_run", payload))
    (build,) = await _builds(session)
    assert (build.trigger, build.pr_number, build.ref) == (BuildTrigger.pull_request, 2, "refs/pull/2/merge")


@pytest.mark.parametrize("conclusion", ["success", "failure"])
async def test_success_and_failure_wait_for_the_run_report(client, session, sessionmaker, settings, repo, conclusion):
    await _send(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID)),
        ("workflow_job", workflow_job(repo, run_id=RUN_ID)),
        ("workflow_run", workflow_run("completed", repo, run_id=RUN_ID, conclusion=conclusion)),
    )
    (build,) = await _builds(session)
    assert build.status is S.running
    assert build.gh_conclusion == conclusion
    assert build.finished_at is None


@pytest.mark.parametrize(
    ("conclusion", "status"),
    [("cancelled", S.cancelled), ("timed_out", S.timed_out), ("startup_failure", S.errored)],
)
async def test_conclusions_settled_by_the_webhook(client, session, sessionmaker, settings, repo, conclusion, status):
    await _send(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID)),
        ("workflow_job", workflow_job(repo, run_id=RUN_ID)),
        ("workflow_run", workflow_run("completed", repo, run_id=RUN_ID, conclusion=conclusion)),
    )
    (build,) = await _builds(session)
    assert (build.status, build.gh_conclusion) == (status, conclusion)
    assert build.finished_at == datetime(2026, 10, 14, 9, 5, tzinfo=timezone.utc)
    assert build.status_reason == f"workflow run concluded: {conclusion}"


# ---------------------------------------------------------- out of order


async def test_job_event_before_its_run_is_retried(client, session, sessionmaker, settings, repo):
    await _send(client, sessionmaker, settings, ("workflow_job", workflow_job(repo, run_id=RUN_ID)))
    assert await _builds(session) == []
    job = (await session.scalars(select(Job))).one()
    event = (await session.scalars(select(WebhookEvent))).one()
    assert job.status is JobStatus.queued and job.attempts == 1
    assert job.run_after > job.created_at  # backed off
    assert "RetryLater: no build yet for run" in job.last_error
    assert event.attempts == 1 and event.last_error == job.last_error and event.processed_at is None

    await _send(client, sessionmaker, settings, ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID)))
    await make_retries_ready(sessionmaker)
    await drain(sessionmaker, settings)

    (build,) = await _builds(session)
    assert build.status is S.running
    session.expunge_all()
    event = await session.get_one(WebhookEvent, event.delivery_id)
    assert event.attempts == 2 and event.last_error is None and event.processed_at is not None


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out"])
@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
async def test_every_delivery_order_ends_in_the_same_state(
    client, session, sessionmaker, settings, repo, conclusion, order
):
    events = [
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID)),
        ("workflow_job", workflow_job(repo, run_id=RUN_ID)),
        ("workflow_run", workflow_run("completed", repo, run_id=RUN_ID, conclusion=conclusion)),
    ]
    for i in order:
        await deliver(client, *events[i])
        await drain(sessionmaker, settings)
    await make_retries_ready(sessionmaker)
    await drain(sessionmaker, settings)

    (build,) = await _builds(session)
    assert build.status is S(conclusion)
    assert build.gh_conclusion == conclusion
    assert await session.scalar(select(func.count()).where(Job.status != JobStatus.done)) == 0


async def test_late_events_do_not_reopen_a_finished_build(client, session, sessionmaker, settings, repo):
    await _send(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("completed", repo, run_id=RUN_ID, conclusion="cancelled")),
        ("workflow_run", workflow_run("in_progress", repo, run_id=RUN_ID)),
        ("workflow_job", workflow_job(repo, run_id=RUN_ID)),
    )
    (build,) = await _builds(session)
    assert build.status is S.cancelled and build.started_at is None


# ----------------------------------------------------------------- re-runs


async def test_rerun_attempt_2_creates_a_new_build(client, session, sessionmaker, settings, repo):
    await _send(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID)),
        ("workflow_job", workflow_job(repo, run_id=RUN_ID)),
        ("workflow_run", workflow_run("completed", repo, run_id=RUN_ID, conclusion="failure")),
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID, attempt=2)),
    )
    first, second = await _builds(session)
    assert (first.number, first.gh_run_attempt, first.status) == (1, 1, S.running)
    assert (second.number, second.gh_run_attempt, second.status) == (2, 2, S.queued)
    assert second.id != first.id
    for field in ("trigger", "commit_sha", "ref", "branch", "profile", "repository_id"):
        assert getattr(second, field) == getattr(first, field)

    # Attempt-2 events touch only attempt 2.
    await _send(
        client, sessionmaker, settings,
        ("workflow_job", workflow_job(repo, run_id=RUN_ID, attempt=2)),
        ("workflow_run", workflow_run("completed", repo, run_id=RUN_ID, attempt=2, conclusion="cancelled")),
    )
    first, second = await _builds(session)
    assert (first.status, first.gh_conclusion) == (S.running, "failure")
    assert (second.status, second.gh_conclusion) == (S.cancelled, "cancelled")


async def test_manual_build_is_bound_by_run_name(client, session, sessionmaker, settings, project, repo):
    manual = await make_build(session, project, trigger=BuildTrigger.manual)
    await session.commit()
    title = f"sfci {manual.id}"

    await _send(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID, event="workflow_dispatch", display_title=title)),
        ("workflow_job", workflow_job(repo, run_id=RUN_ID)),
    )
    (build,) = await _builds(session)
    assert build.id == manual.id
    assert (build.gh_run_id, build.gh_run_attempt, build.status) == (RUN_ID, 1, S.running)

    # Re-running the manual run is a new manual build.
    await _send(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID, attempt=2, event="workflow_dispatch", display_title=title)),
    )
    first, second = await _builds(session)
    assert first.id == manual.id and second.gh_run_attempt == 2
    assert second.trigger is BuildTrigger.manual


# ----------------------------------------------------------------- ignored


async def test_untracked_runs_are_ignored(client, session, sessionmaker, settings, repo):
    other_repo = workflow_run("requested", repo, run_id=1, gh_repo_id=999)
    other_repo["repository"]["full_name"] = "someone/else"
    await _send(
        client, sessionmaker, settings,
        ("workflow_run", other_repo),
        ("workflow_run", workflow_run("requested", repo, run_id=2, path=".github/workflows/toolchain-image.yml")),
        ("workflow_run", workflow_run("requested", repo, run_id=3, event="workflow_dispatch")),
        ("workflow_run", workflow_run("requested", repo, run_id=4, event="schedule")),
    )
    assert await _builds(session) == []
    assert await session.scalar(select(func.count()).where(WebhookEvent.processed_at.is_(None))) == 0


async def test_job_events_of_untracked_runs_are_not_retried(client, session, sessionmaker, settings, repo):
    toolchain = ".github/workflows/toolchain-image.yml"
    await _send(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("requested", repo, run_id=2, path=toolchain)),
        ("workflow_job", workflow_job(repo, run_id=2)),
    )
    assert await _builds(session) == []
    job = (await session.scalars(select(Job).order_by(Job.id.desc()))).first()
    assert (job.status, job.attempts, job.last_error) == (JobStatus.done, 1, None)


async def test_job_event_of_a_later_attempt_still_waits_for_its_run(client, session, sessionmaker, settings, repo):
    # Attempt 1 is tracked; attempt 2's workflow_run has not arrived yet.
    await _send(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID)),
        ("workflow_job", workflow_job(repo, run_id=RUN_ID, attempt=2)),
    )
    job = (await session.scalars(select(Job).order_by(Job.id.desc()))).first()
    assert job.status is JobStatus.queued and "RetryLater" in job.last_error


# ---------------------------------------------------------------- approval


async def test_a_run_awaiting_approval_continues_once_approved(client, session, sessionmaker, settings, repo):
    await _send(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID, event="pull_request")),
        ("workflow_run", workflow_run("completed", repo, run_id=RUN_ID, event="pull_request", conclusion="action_required")),
    )
    (build,) = await _builds(session)
    assert (build.status, build.gh_conclusion, build.finished_at) == (S.queued, "action_required", None)

    # Approved: the same run attempt starts.
    await _send(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("in_progress", repo, run_id=RUN_ID, event="pull_request")),
        ("workflow_job", workflow_job(repo, run_id=RUN_ID)),
    )
    (build,) = await _builds(session)
    assert (build.status, build.gh_conclusion) == (S.running, None)

    await _send(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("completed", repo, run_id=RUN_ID, event="pull_request", conclusion="cancelled")),
    )
    (build,) = await _builds(session)
    assert (build.status, build.gh_conclusion) == (S.cancelled, "cancelled")


# ---------------------------------------------------------- worker errors


async def test_failing_job_records_error_then_gives_up(client, session, sessionmaker, settings, repo):
    payload = workflow_run("requested", repo, run_id=RUN_ID)
    del payload["workflow_run"]["run_attempt"]  # malformed: the handler raises
    await deliver(client, "workflow_run", payload, delivery_id="bad-1")
    async with sessionmaker() as s:
        await s.execute(update(Job).values(max_attempts=2))
        await s.commit()

    await drain(sessionmaker, settings)
    job = (await session.scalars(select(Job))).one()
    assert (job.status, job.attempts) == (JobStatus.queued, 1)
    assert job.last_error == "KeyError: 'run_attempt'"

    await make_retries_ready(sessionmaker)
    await drain(sessionmaker, settings)
    session.expunge_all()
    job = (await session.scalars(select(Job))).one()
    event = await session.get_one(WebhookEvent, "bad-1")
    assert (job.status, job.attempts) == (JobStatus.failed, 2)
    assert (event.attempts, event.last_error, event.processed_at) == (2, "KeyError: 'run_attempt'", None)
    assert await _builds(session) == []  # the failed handler's writes were rolled back


def test_backoff_doubles_and_caps():
    assert [backoff(n).total_seconds() for n in (1, 2, 3, 4)] == [5, 10, 20, 40]
    assert backoff(30) == timedelta(minutes=15)
