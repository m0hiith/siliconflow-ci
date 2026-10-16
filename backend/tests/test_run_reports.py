"""POST /api/v1/runs/report, and how the report and the run's conclusion
decide a build's final status."""
import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select, text

from app.models import (
    AuditLog,
    Build,
    BuildStage,
    BuildStatus,
    BuildTrigger,
    Job,
    JobStatus,
    Repository,
    RunReport,
)
from app.services.github import GitHubApp
from tests import oidc, reports
from tests.conftest import GITHUB_API
from tests.factories import make_build, make_project
from tests.github import SHA, deliver, drain, make_retries_ready, workflow_job, workflow_run
from tests.reports import RUN_ID, make_run_build, post

S = BuildStatus
OTHER_SHA = "0" * 39 + "1"


@pytest.fixture(scope="module")
def key():
    return oidc.new_key()


@pytest.fixture
def mock(key):
    with respx.mock(assert_all_called=False) as router:
        router.get(oidc.JWKS_URL).respond(json=oidc.jwks((key, oidc.KID)))
        yield router


@pytest.fixture
async def project(session):
    project = await make_project(session, "fw")
    await session.commit()
    return project


@pytest.fixture
def repo(project) -> Repository:
    return project.repositories[0]


def no_files(**overrides):
    """A report with nothing to download, so no ingestion job is queued."""
    return reports.report(artifact_name=None, manifest=None, junit=[], **overrides)


async def _build(session, build_id) -> Build:
    session.expunge_all()
    return await session.get_one(Build, build_id)


async def _count(session, model, *where) -> int:
    return await session.scalar(select(func.count()).select_from(model).where(*where))


# ------------------------------------------------------------- accepting


async def test_report_is_stored_with_its_stages(client, session, mock, key, project):
    build = await make_run_build(session, project)
    body = reports.report()

    r = await post(client, body, reports.token(key))
    assert r.status_code == 202, r.text
    assert r.json() == {"status": "accepted", "build_id": build.id}

    stages = (await client.get(f"/api/v1/builds/{build.id}/stages")).json()
    assert [(s["name"], s["position"], s["required"], s["status"], s["exit_code"]) for s in stages] == [
        ("configure", 0, True, "passed", 0),
        ("compile", 1, True, "passed", 0),
        ("test", 2, True, "passed", 0),
        ("analyze", 3, True, "passed", 0),
        ("package", 4, True, "passed", 0),
    ]
    assert stages[1]["started_at"] == "2026-10-14T09:01:04Z"

    stored = (await session.scalars(select(RunReport))).one()
    assert stored.build_id == build.id and stored.schema_version == "sfci.run-report/1"
    assert stored.payload["coverage"]["line_percent"] == 91.25
    assert stored.payload["toolchain"] == body["toolchain"]
    assert stored.claims["run_id"] == RUN_ID and stored.claims["repository"] == "acme/fw"
    assert stored.artifacts_state == "pending"
    assert "eyJ" not in str(stored.claims)  # no token is kept

    job = (await session.scalars(select(Job))).one()
    assert (job.kind, job.payload) == ("ingest_artifacts", {"build_id": build.id})
    # The run has not concluded, so the build is not decided yet.
    assert (await _build(session, build.id)).status is S.running


async def test_backend_decides_which_stages_are_required(client, session, mock, key, project):
    build = await make_run_build(session, project)
    body = no_files()
    body["stages"] = [
        {"name": "configure", "status": "passed", "exit_code": 0, "required": True},
        {"name": "lint", "status": "failed", "exit_code": 2, "required": False},
        # The runner says "not required"; the backend's list says otherwise.
        {"name": "test", "status": "failed", "exit_code": 1, "required": False},
        {"name": "docs", "status": "passed", "exit_code": 0, "required": True},
    ]
    assert (await post(client, body, reports.token(key))).status_code == 202

    rows = (await session.scalars(select(BuildStage).order_by(BuildStage.position))).all()
    assert [(s.name, s.required, s.status.value) for s in rows] == [
        ("configure", True, "passed"),
        ("lint", False, "failed"),
        ("test", True, "failed"),
        ("docs", True, "passed"),  # a report may add required stages
        # Required stages the report never mentioned did not run.
        ("compile", True, "skipped"),
        ("analyze", True, "skipped"),
        ("package", True, "skipped"),
    ]
    assert await _count(session, Job) == 0  # nothing to download


async def test_report_for_a_queued_build_starts_it(client, session, mock, key, project):
    build = await make_run_build(session, project, status=S.queued)
    assert (await post(client, no_files(), reports.token(key))).status_code == 202
    build = await _build(session, build.id)
    assert build.status is S.running
    assert build.started_at == datetime(2026, 10, 14, 9, 1, tzinfo=UTC)  # first stage start


@pytest.mark.parametrize(
    ("job", "profile"),
    [
        ("host-cmake", "host-cmake"),  # the job that ran becomes the profile
        (None, "firmware-ci"),  # no job named: the workflow's name stays
        ("host cmake; rm", "firmware-ci"),  # not a usable profile name
    ],
)
async def test_report_job_becomes_the_profile(client, session, mock, key, project, job, profile):
    build = await make_run_build(session, project, profile="firmware-ci")
    body = no_files()
    body["github"]["job"] = job
    assert (await post(client, body, reports.token(key))).status_code == 202
    assert (await _build(session, build.id)).profile == profile


SIWX917_WORKFLOW = ".github/workflows/firmware-ci-siwx917-soc.yml"
SIWX917_SDK = {"simplicity_sdk": "2026.6.1", "wiseconnect": "4.1.1"}
SIWX917_TOOLCHAIN = {"slc_cli": "6.0.23", "arm-none-eabi-gcc": "14.2.1"}


async def test_siwx917_report_keeps_sdk_and_its_own_required_stages(client, session, mock, key, project):
    build = await make_run_build(session, project, profile="firmware-ci-siwx917-soc")
    body = no_files(sdk=SIWX917_SDK, toolchain=SIWX917_TOOLCHAIN)
    body["github"]["job"] = "siwx917-soc"
    body["stages"] = [
        {"name": "generate", "status": "passed", "exit_code": 0, "required": True},
        {"name": "compile", "status": "failed", "exit_code": 2, "required": True},
    ]
    token = reports.token(key, workflow_path=SIWX917_WORKFLOW)
    assert (await post(client, body, token)).status_code == 202

    rows = (await session.scalars(select(BuildStage).order_by(BuildStage.position))).all()
    assert [(s.name, s.required, s.status.value) for s in rows] == [
        ("generate", True, "passed"),
        ("compile", True, "failed"),
        ("package", True, "skipped"),
    ]
    assert (await _build(session, build.id)).profile == "siwx917-soc"
    stored = (await session.scalars(select(RunReport))).one()
    assert stored.payload["sdk"] == SIWX917_SDK

    detail = (await client.get(f"/api/v1/builds/{build.id}")).json()
    assert detail["sdk"] == SIWX917_SDK
    assert detail["toolchain"] == SIWX917_TOOLCHAIN


async def test_report_without_sdk_shows_none(client, session, mock, key, project):
    build = await make_run_build(session, project)
    assert (await post(client, no_files(), reports.token(key))).status_code == 202
    detail = (await client.get(f"/api/v1/builds/{build.id}")).json()
    assert detail["sdk"] == {}
    assert detail["toolchain"]["gcc"]


async def test_identical_retry_is_a_duplicate(client, session, mock, key, project):
    build = await make_run_build(session, project)
    body = reports.report()
    assert (await post(client, body, reports.token(key))).status_code == 202
    r = await post(client, body, reports.token(key))
    assert r.status_code == 200 and r.json() == {"status": "duplicate", "build_id": build.id}
    assert await _count(session, RunReport) == 1
    assert await _count(session, Job) == 1


async def test_a_different_second_report_is_refused(client, session, mock, key, project):
    await make_run_build(session, project)
    assert (await post(client, reports.report(), reports.token(key))).status_code == 202
    r = await post(client, reports.with_stage(reports.report(), "test", "failed"), reports.token(key))
    assert r.status_code == 409
    assert (await session.scalars(select(RunReport))).one().payload["stages"][2]["status"] == "passed"


# ---------------------------------------------------------- authentication


async def test_missing_or_non_bearer_token_is_401(client, session, mock, project):
    await make_run_build(session, project)
    for headers in ({}, {"authorization": "Basic YTpi"}, {"authorization": "Bearer "}):
        r = await client.post("/api/v1/runs/report", json=reports.report(), headers=headers)
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
    assert await _count(session, RunReport) == 0


async def test_forged_token_is_401(client, session, mock, project):
    await make_run_build(session, project)
    forger = oidc.new_key()
    r = await post(client, reports.report(), reports.token(forger))
    assert r.status_code == 401
    assert await _count(session, RunReport) == 0


@pytest.mark.parametrize("claim", [{"aud": "someone-else"}, {"iss": "https://example.com"}])
async def test_token_for_another_audience_or_issuer_is_401(client, session, mock, key, project, claim):
    await make_run_build(session, project)
    r = await post(client, reports.report(), reports.token(key, **claim))
    assert r.status_code == 401


async def test_token_is_checked_before_the_body(client, mock, key):
    r = await post(client, {"schema": "nonsense"}, None)
    assert r.status_code == 401  # an anonymous caller learns nothing about the schema
    r = await post(client, {"schema": "nonsense"}, reports.token(key))
    assert r.status_code == 422


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(schema="sfci.run-report/2"),
        lambda b: b.update(surprise=True),
        lambda b: b["stages"].append(dict(b["stages"][0])),  # duplicate stage
        lambda b: b["stages"][0].update(status="exploded"),
        lambda b: b["stages"][0].update(name="../etc"),
        lambda b: b["stages"][0].update(started_at="2026-10-14T10:00:00Z"),  # after finished_at
        lambda b: b["manifest"]["files"][0].update(file="../../etc/passwd"),
        lambda b: b["manifest"]["files"][0].update(file="/abs"),
        lambda b: b["manifest"]["files"][0].update(sha256="ABC"),
        lambda b: b["junit"][0].update(path="junit/./x.xml"),
        lambda b: b.update(artifact_name=None),  # files but nowhere to fetch them
        lambda b: b["coverage"].update(line_percent=101),
        lambda b: b.update(sdk={"wiseconnect": 4}),
        lambda b: b.update(sdk=["2026.6.1"]),
        lambda b: b.update(sdk={f"sdk{i}": "1.0" for i in range(17)}),
    ],
)
async def test_malformed_reports_are_422(client, session, mock, key, project, mutate):
    await make_run_build(session, project)
    body = reports.report()
    mutate(body)
    assert (await post(client, body, reports.token(key))).status_code == 422
    assert await _count(session, RunReport) == 0


async def test_unreachable_jwks_is_503(client, key):
    with respx.mock() as router:
        router.get(oidc.JWKS_URL).mock(side_effect=httpx.ConnectError("down"))
        r = await post(client, reports.report(), reports.token(key))
    assert r.status_code == 503


# ------------------------------------------------------- matching the build


@pytest.mark.parametrize(
    ("body", "token_claims", "detail"),
    [
        ({"run_id": RUN_ID + 1}, {}, "report does not describe the run its token was issued to"),
        ({"run_attempt": 2}, {}, "report does not describe the run its token was issued to"),
        ({"sha": OTHER_SHA}, {}, "report does not describe the run its token was issued to"),
        ({}, {"workflow_path": ".github/workflows/toolchain-image.yml"}, "token is not from a firmware workflow"),
        ({"sha": OTHER_SHA}, {"sha": OTHER_SHA}, "token commit does not match the build"),
        ({}, {"repository_id": 1}, "token repository id does not match the build"),
    ],
)
async def test_mismatched_reports_are_403(client, session, mock, key, project, repo, body, token_claims, detail):
    repo = await session.get_one(Repository, repo.id)
    repo.gh_repo_id = 300029405
    await make_run_build(session, project)
    r = await post(client, reports.report(**body), reports.token(key, **token_claims))
    assert r.status_code == 403 and r.json()["detail"] == detail
    assert await _count(session, RunReport) == 0


async def test_token_from_another_repository_is_403(client, session, mock, key, project):
    # Same run id, but the token (and report) are for someone else's repository.
    await make_run_build(session, project)
    body = reports.report()
    body["github"]["repository"] = "mallory/fw"
    r = await post(client, body, reports.token(key, repository="mallory/fw", repository_id=None))
    assert r.status_code == 403 and r.json()["detail"] == "token repository does not match the build"


async def test_pull_request_report_is_matched_by_merge_ref(client, session, mock, key, project):
    # PR jobs run on GitHub's merge commit; the build records the PR head.
    merge_sha = "f" * 40
    await make_run_build(session, project, trigger=BuildTrigger.pull_request, pr_number=2)
    build = (await session.scalars(select(Build))).one()
    build.ref = "refs/pull/2/merge"
    await session.commit()

    wrong = reports.token(key, sha=merge_sha, ref="refs/pull/3/merge", event_name="pull_request")
    r = await post(client, no_files(sha=merge_sha), wrong)
    assert r.status_code == 403 and r.json()["detail"] == "token ref does not match the build"

    right = reports.token(key, sha=merge_sha, ref="refs/pull/2/merge", event_name="pull_request")
    assert (await post(client, no_files(sha=merge_sha), right)).status_code == 202


async def test_unknown_run_is_404(client, session, mock, key, project):
    await make_run_build(session, project)
    r = await post(client, no_files(run_id=999), reports.token(key, run_id=999))
    assert r.status_code == 404


@pytest.mark.parametrize("status", [S.passed, S.failed, S.cancelled, S.timed_out, S.errored])
async def test_report_for_a_finished_build_is_409(client, session, mock, key, project, status):
    build = await make_run_build(session, project, status=status)
    r = await post(client, reports.report(), reports.token(key))
    assert r.status_code == 409 and r.json()["detail"] == f"build is already {status.value}"
    assert await _count(session, RunReport) == 0
    assert await _count(session, BuildStage) == 0
    assert (await _build(session, build.id)).status is status


# ------------------------------------------------------------ final status


async def _webhooks(client, sessionmaker, settings, *events):
    for event, payload in events:
        assert (await deliver(client, event, payload)).status_code == 202
    await drain(sessionmaker, settings)


def _started(repo):
    return [
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID)),
        ("workflow_job", workflow_job(repo, run_id=RUN_ID)),
    ]


def _completed(repo, conclusion):
    return ("workflow_run", workflow_run("completed", repo, run_id=RUN_ID, conclusion=conclusion))


async def _only_build(session) -> Build:
    session.expunge_all()
    return (await session.scalars(select(Build))).one()


async def test_report_then_success_is_passed(client, session, sessionmaker, settings, mock, key, repo):
    await _webhooks(client, sessionmaker, settings, *_started(repo))
    assert (await post(client, no_files(), reports.token(key))).status_code == 202
    assert (await _only_build(session)).status is S.running

    await _webhooks(client, sessionmaker, settings, _completed(repo, "success"))
    build = await _only_build(session)
    assert build.status is S.passed
    assert build.status_reason == "workflow run succeeded and every required stage passed"
    assert build.finished_at == datetime(2026, 10, 14, 9, 5, tzinfo=UTC)
    assert await _count(session, Job, Job.kind == "report_deadline") == 0


async def test_success_then_report_is_passed(client, session, sessionmaker, settings, mock, key, repo):
    await _webhooks(client, sessionmaker, settings, *_started(repo), _completed(repo, "success"))
    assert (await _only_build(session)).status is S.running
    assert (await post(client, no_files(), reports.token(key))).status_code == 202
    assert (await _only_build(session)).status is S.passed

    # The deadline job queued by "completed" finds nothing left to do.
    await make_retries_ready(sessionmaker)
    await drain(sessionmaker, settings)
    assert (await _only_build(session)).status is S.passed


async def test_failed_stage_with_failure_is_failed(client, session, sessionmaker, settings, mock, key, repo):
    await _webhooks(client, sessionmaker, settings, *_started(repo))
    body = reports.with_stage(no_files(), "test", "failed", exit_code=8)
    assert (await post(client, body, reports.token(key))).status_code == 202
    await _webhooks(client, sessionmaker, settings, _completed(repo, "failure"))
    build = await _only_build(session)
    assert build.status is S.failed
    assert build.status_reason == "required stages did not pass: test (failed)"


async def test_success_with_a_skipped_required_stage_is_failed(client, session, sessionmaker, settings, mock, key, repo):
    await _webhooks(client, sessionmaker, settings, *_started(repo))
    body = no_files()
    body["stages"] = [s for s in body["stages"] if s["name"] != "analyze"]
    assert (await post(client, body, reports.token(key))).status_code == 202
    await _webhooks(client, sessionmaker, settings, _completed(repo, "success"))
    build = await _only_build(session)
    assert build.status is S.failed
    assert build.status_reason == "required stages did not pass: analyze (skipped)"


async def test_failure_outside_the_stages_is_failed(client, session, sessionmaker, settings, mock, key, repo):
    await _webhooks(client, sessionmaker, settings, *_started(repo))
    assert (await post(client, no_files(), reports.token(key))).status_code == 202
    await _webhooks(client, sessionmaker, settings, _completed(repo, "failure"))
    build = await _only_build(session)
    assert build.status is S.failed
    assert build.status_reason == "workflow run failed outside the required stages"


@pytest.mark.parametrize("conclusion", ["success", "failure"])
async def test_no_report_by_the_deadline_is_errored(client, session, sessionmaker, settings, mock, key, repo, conclusion):
    await _webhooks(client, sessionmaker, settings, *_started(repo), _completed(repo, conclusion))
    assert (await _only_build(session)).status is S.running

    job = (await session.scalars(select(Job).where(Job.kind == "report_deadline"))).one()
    assert job.payload == {"build_id": (await _only_build(session)).id}
    wait = job.run_after - job.created_at
    assert timedelta(minutes=9, seconds=59) < wait <= timedelta(minutes=10, seconds=1)
    assert await drain(sessionmaker, settings) == 0  # not yet due

    await make_retries_ready(sessionmaker)
    await drain(sessionmaker, settings)
    build = await _only_build(session)
    assert build.status is S.errored
    assert build.status_reason == (
        f"workflow run concluded {conclusion} but no run report arrived within 10 minutes"
    )

    # A report that turns up afterwards cannot reopen the build.
    r = await post(client, no_files(), reports.token(key))
    assert r.status_code == 409 and r.json()["detail"] == "build is already errored"


async def _rerun_without_a_report(client, sessionmaker, settings, session, repo) -> Build:
    """Attempt 2 of the run succeeds and posts no report; its deadline passes."""
    await _webhooks(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID, attempt=2)),
        ("workflow_run", workflow_run("completed", repo, run_id=RUN_ID, attempt=2, conclusion="success")),
    )
    await make_retries_ready(sessionmaker)
    await drain(sessionmaker, settings)
    session.expunge_all()
    return (await session.scalars(select(Build).where(Build.gh_run_attempt == 2))).one()


async def test_rerunning_only_the_failed_helper_job_inherits_the_report(
    client, session, sessionmaker, settings, mock, key, repo
):
    # firmware-ci.yml has two jobs: host-cmake (posts the report) and
    # ci-scripts. If only ci-scripts fails, "Re-run failed jobs" starts attempt
    # 2 with ci-scripts alone; GitHub reuses host-cmake's attempt-1 result, so
    # attempt 2 gets no report of its own.
    await _webhooks(client, sessionmaker, settings, *_started(repo))
    assert (await post(client, no_files(), reports.token(key))).status_code == 202
    await _webhooks(client, sessionmaker, settings, _completed(repo, "failure"))
    first = await _only_build(session)
    assert first.status is S.failed

    second = await _rerun_without_a_report(client, sessionmaker, settings, session, repo)
    assert second.status is S.passed, f"{second.status.value}: {second.status_reason}"
    report = (await session.scalars(select(RunReport).where(RunReport.build_id == second.id))).one()
    assert report.payload["github"]["run_attempt"] == 1  # where it really came from
    assert await _count(session, BuildStage, BuildStage.build_id == second.id) == await _count(
        session, BuildStage, BuildStage.build_id == first.id
    )
    audit = (await session.scalars(select(AuditLog).where(AuditLog.action == "run_report.inherited"))).one()
    assert (audit.target_id, audit.details) == (second.id, {"from_build_id": first.id, "from_attempt": 1})


async def test_a_rerun_does_not_inherit_a_report_with_a_failed_required_stage(
    client, session, sessionmaker, settings, mock, key, repo
):
    # host-cmake itself failed in attempt 1, so a re-run runs it again and it
    # must post its own report; without one the build is errored.
    await _webhooks(client, sessionmaker, settings, *_started(repo))
    body = reports.with_stage(no_files(), "test", "failed", exit_code=8)
    assert (await post(client, body, reports.token(key))).status_code == 202
    await _webhooks(client, sessionmaker, settings, _completed(repo, "failure"))

    second = await _rerun_without_a_report(client, sessionmaker, settings, session, repo)
    assert second.status is S.errored
    assert await _count(session, RunReport, RunReport.build_id == second.id) == 0


async def _advisory_waiters(session, n: int) -> None:
    for _ in range(500):
        waiting = await session.scalar(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
        )
        if waiting >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {n} transactions waiting on the run's advisory lock, saw {waiting}")


async def test_a_report_racing_the_completed_webhook_still_settles_the_build(
    client, session, sessionmaker, settings, mock, key, repo
):
    await _webhooks(client, sessionmaker, settings, *_started(repo))
    assert (await deliver(client, *_completed(repo, "success"))).status_code == 202

    # Hold the run's lock so the report and the webhook job both reach it, then
    # release them together. Each must see the other's commit, or the build
    # would wait for the deadline job.
    async with sessionmaker() as holder:
        await holder.execute(select(func.pg_advisory_lock(RUN_ID)))
        report = asyncio.create_task(post(client, no_files(), reports.token(key)))
        worker = asyncio.create_task(drain(sessionmaker, settings))
        await _advisory_waiters(holder, 2)
        await holder.execute(select(func.pg_advisory_unlock(RUN_ID)))
        await holder.commit()
        response, _ = await asyncio.gather(report, worker)

    assert response.status_code == 202
    build = await _only_build(session)
    assert build.status is S.passed, f"{build.status.value}: {build.status_reason}"


async def test_webhook_settled_conclusions_ignore_the_report(client, session, sessionmaker, settings, mock, key, repo):
    await _webhooks(client, sessionmaker, settings, *_started(repo))
    assert (await post(client, no_files(), reports.token(key))).status_code == 202
    await _webhooks(client, sessionmaker, settings, _completed(repo, "cancelled"))
    assert (await _only_build(session)).status is S.cancelled
    assert await _count(session, Job, Job.kind == "report_deadline") == 0


async def test_report_before_any_job_event_settles_from_queued(client, session, sessionmaker, settings, mock, key, repo):
    # workflow_job never arrives: the build is still queued at completion.
    await _webhooks(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID)),
        _completed(repo, "success"),
    )
    assert (await _only_build(session)).status is S.queued
    assert (await post(client, no_files(), reports.token(key))).status_code == 202
    build = await _only_build(session)
    assert build.status is S.passed
    assert build.started_at is not None and build.finished_at >= build.started_at


async def test_manual_build_learns_its_commit_and_accepts_its_report(
    client, session, sessionmaker, settings, mock, key, project, repo
):
    manual = await make_build(session, project, trigger=BuildTrigger.manual)
    await session.commit()
    assert manual.commit_sha is None
    await _webhooks(
        client, sessionmaker, settings,
        ("workflow_run", workflow_run(
            "requested", repo, run_id=RUN_ID, event="workflow_dispatch", display_title=f"sfci {manual.id}"
        )),
    )
    build = await _only_build(session)
    assert build.id == manual.id and build.commit_sha == SHA
    assert build.commit_message == "build(test): check workflow run event payload"
    r = await post(client, no_files(), reports.token(key, event_name="workflow_dispatch"))
    assert r.status_code == 202


async def test_all_jobs_finish_cleanly(client, session, sessionmaker, settings, mock, key, repo):
    # A passed build has its release gates evaluated, which reads gates.yml.
    mock.get(f"{GITHUB_API}/repos/acme/fw/installation").respond(json={"id": 42})
    mock.post(f"{GITHUB_API}/app/installations/42/access_tokens").respond(
        201,
        json={"token": "ghs_x", "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    )
    mock.get(f"{GITHUB_API}/repos/acme/fw/contents/.siliconflow/gates.yml").respond(404)
    github = GitHubApp(settings)
    try:
        await _webhooks(client, sessionmaker, settings, *_started(repo))
        await post(client, no_files(), reports.token(key))
        await _webhooks(client, sessionmaker, settings, _completed(repo, "success"))
        await make_retries_ready(sessionmaker)
        await drain(sessionmaker, settings, github)
    finally:
        await github.aclose()
    assert await _count(session, Job, Job.status != JobStatus.done) == 0
    assert await _count(session, Job, Job.kind == "evaluate_gates") == 1
