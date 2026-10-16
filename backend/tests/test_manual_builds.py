"""POST /projects/{id}/builds and POST /builds/{id}/cancel, against a
respx-mocked GitHub."""
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.auth import get_current_user
from app.main import create_app
from app.models import AuditLog, Build, BuildStatus, BuildTrigger, User, UserRole
from tests.conftest import GITHUB_API
from tests.factories import make_build, make_project
from tests.github import deliver, drain, workflow_job, workflow_run

S = BuildStatus
INSTALLATION = 77
RUN_ID = 555001
DISPATCH = "/repos/acme/fw/actions/workflows/firmware-ci.yml/dispatches"
CANCEL = f"/repos/acme/fw/actions/runs/{RUN_ID}/cancel"


@pytest.fixture
def github():
    with respx.mock(base_url=GITHUB_API, assert_all_called=False) as router:
        expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        router.post(f"/app/installations/{INSTALLATION}/access_tokens").mock(
            return_value=httpx.Response(201, json={"token": "ghs_test", "expires_at": expires_at})
        )
        yield router


@pytest.fixture
async def project(session):
    project = await make_project(session, "fw")
    project.repositories[0].gh_installation_id = INSTALLATION
    await session.commit()
    return project


@pytest.fixture
def repo(project):
    return project.repositories[0]


async def _user(session, github_id: int, login: str, role: UserRole) -> User:
    user = User(github_id=github_id, login=login, role=role)
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
async def maintainer(app, session):
    user = await _user(session, 1, "maint", UserRole.maintainer)
    app.dependency_overrides[get_current_user] = lambda: user
    return user


@pytest.fixture
async def viewer(app, session):
    user = await _user(session, 2, "view", UserRole.viewer)
    app.dependency_overrides[get_current_user] = lambda: user
    return user


async def _builds(session) -> list[Build]:
    # Re-read rows the API changed without expiring other loaded objects.
    stmt = select(Build).order_by(Build.number).execution_options(populate_existing=True)
    return list((await session.scalars(stmt)).all())


async def _audit(session) -> list[AuditLog]:
    return list((await session.scalars(select(AuditLog).order_by(AuditLog.id))).all())


def _trigger(client, project, **body):
    return client.post(f"/api/v1/projects/{project.id}/builds", json=body)


# ----------------------------------------------------------------- authz


async def test_trigger_requires_a_user(client, project, github):
    r = await _trigger(client, project)
    assert r.status_code == 401
    assert not github.calls


async def test_trigger_requires_maintainer(client, session, project, viewer, github):
    r = await _trigger(client, project)
    assert r.status_code == 403
    assert await _builds(session) == [] and not github.calls


async def test_cancel_requires_maintainer(client, session, project, viewer, github):
    build = await make_build(session, project, gh_run_id=RUN_ID, gh_run_attempt=1)
    await session.commit()
    r = await client.post(f"/api/v1/builds/{build.id}/cancel")
    assert r.status_code == 403 and not github.calls


async def test_unconfigured_app_is_503_and_creates_nothing(settings, engine, session, project, maintainer):
    app = create_app(settings.model_copy(update={"github_app_private_key": None}))
    app.dependency_overrides[get_current_user] = lambda: maintainer
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(f"/api/v1/projects/{project.id}/builds", json={})
        assert r.status_code == 503
        assert await _builds(session) == []
    finally:
        await app.state.github.aclose()
        await app.state.oidc.aclose()
        await app.state.engine.dispose()


# --------------------------------------------------------------- trigger


async def test_trigger_creates_queued_manual_build_and_dispatches(
    client, session, project, repo, maintainer, github
):
    dispatch = github.post(DISPATCH).mock(return_value=httpx.Response(204))

    r = await _trigger(client, project)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "queued" and body["trigger"] == "manual"
    assert body["ref"] == "refs/heads/main" and body["branch"] == "main"
    assert body["profile"] == "firmware-ci" and body["number"] == 1

    request = dispatch.calls.last.request
    assert request.headers["authorization"] == "Bearer ghs_test"
    assert json.loads(request.content) == {"ref": "main", "inputs": {"build_id": body["id"]}}

    (build,) = await _builds(session)
    assert build.id == body["id"] and build.triggered_by_user_id == maintainer.id
    (audit,) = await _audit(session)
    assert (audit.action, audit.target_type, audit.target_id) == ("build.trigger", "build", build.id)
    assert audit.actor_user_id == maintainer.id
    assert audit.details["branch"] == "main" and audit.details["repository"] == "acme/fw"


async def test_trigger_on_named_branch(client, project, maintainer, github):
    dispatch = github.post(DISPATCH).mock(return_value=httpx.Response(204))
    r = await _trigger(client, project, branch="release/1.2")
    assert r.status_code == 201
    assert r.json()["ref"] == "refs/heads/release/1.2"
    assert json.loads(dispatch.calls.last.request.content)["ref"] == "release/1.2"


@pytest.mark.parametrize("branch", ["refs/heads/main", "has space", "a..b", "-x", ""])
async def test_trigger_rejects_non_branch_names(client, project, maintainer, github, branch):
    r = await _trigger(client, project, branch=branch)
    assert r.status_code == 422 and not github.calls


async def test_trigger_looks_up_and_stores_installation_id(client, session, project, repo, maintainer, github):
    repo.gh_installation_id = None
    await session.commit()
    lookup = github.get("/repos/acme/fw/installation").mock(
        return_value=httpx.Response(200, json={"id": INSTALLATION})
    )
    github.post(DISPATCH).mock(return_value=httpx.Response(204))

    assert (await _trigger(client, project)).status_code == 201
    assert (await _trigger(client, project)).status_code == 201
    assert lookup.call_count == 1
    await session.refresh(repo)
    assert repo.gh_installation_id == INSTALLATION


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(422, json={"message": "No ref found for: nope"}), "422 (No ref found for: nope)"),
        (httpx.Response(404, json={"message": "Not Found"}), "404 (Not Found)"),
        (httpx.ConnectTimeout("slow"), "ConnectTimeout"),
    ],
)
async def test_failed_dispatch_errors_the_build(client, session, project, maintainer, github, response, reason):
    if isinstance(response, Exception):
        github.post(DISPATCH).mock(side_effect=response)
    else:
        github.post(DISPATCH).mock(return_value=response)

    r = await _trigger(client, project)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "errored"
    assert body["status_reason"].startswith("workflow dispatch failed")
    assert reason in body["status_reason"]
    assert "ghs_test" not in body["status_reason"]

    (build,) = await _builds(session)
    assert build.status is S.errored and build.finished_at is not None
    assert [a.action for a in await _audit(session)] == ["build.trigger"]


async def test_failed_installation_lookup_errors_the_build(client, session, project, repo, maintainer, github):
    repo.gh_installation_id = None
    await session.commit()
    github.get("/repos/acme/fw/installation").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    r = await _trigger(client, project)
    assert r.json()["status"] == "errored"
    assert "installation lookup" in r.json()["status_reason"]


async def test_trigger_unknown_project_or_repository(client, session, project, maintainer, github):
    r = await client.post("/api/v1/projects/prj_NOPE/builds", json={})
    assert r.status_code == 404
    other = await make_project(session, "other")
    await session.commit()
    r = await _trigger(client, project, repository_id=other.repositories[0].id)
    assert r.status_code == 422
    assert await _builds(session) == [] and not github.calls


# ---------------------------------------------- correlation and cancel


async def test_dispatched_build_is_bound_by_run_name_then_cancelled(
    client, session, sessionmaker, settings, project, repo, maintainer, github
):
    github.post(DISPATCH).mock(return_value=httpx.Response(204))
    cancel = github.post(CANCEL).mock(return_value=httpx.Response(202))
    build_id = (await _trigger(client, project)).json()["id"]

    # Before the run is known, there is nothing to cancel on GitHub.
    r = await client.post(f"/api/v1/builds/{build_id}/cancel")
    assert r.status_code == 409 and not cancel.called

    title = f"sfci {build_id}"
    for event, payload in [
        ("workflow_run", workflow_run("requested", repo, run_id=RUN_ID, event="workflow_dispatch", display_title=title)),
        ("workflow_job", workflow_job(repo, run_id=RUN_ID)),
    ]:
        assert (await deliver(client, event, payload)).status_code == 202
    await drain(sessionmaker, settings)
    (build,) = await _builds(session)
    assert (build.id, build.gh_run_id, build.status) == (build_id, RUN_ID, S.running)

    r = await client.post(f"/api/v1/builds/{build_id}/cancel")
    assert r.status_code == 202, r.text
    # Asking is not doing: status waits for the webhook.
    assert r.json()["status"] == "running"
    assert cancel.calls.last.request.headers["authorization"] == "Bearer ghs_test"
    assert [a.action for a in await _audit(session)] == ["build.trigger", "build.cancel_request"]

    await deliver(
        client,
        "workflow_run",
        workflow_run("completed", repo, run_id=RUN_ID, event="workflow_dispatch", display_title=title, conclusion="cancelled"),
    )
    await drain(sessionmaker, settings)
    (build,) = await _builds(session)
    assert build.status is S.cancelled and build.trigger is BuildTrigger.manual


async def test_cancel_finished_build_is_409(client, session, project, maintainer, github):
    build = await make_build(session, project, gh_run_id=RUN_ID, gh_run_attempt=1)
    build.status = S.passed
    await session.commit()
    r = await client.post(f"/api/v1/builds/{build.id}/cancel")
    assert r.status_code == 409 and not github.calls


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (httpx.Response(409, json={"message": "Cannot cancel a workflow run that is completed."}), 409),
        (httpx.Response(500), 502),
        (httpx.ConnectError("down"), 502),
    ],
)
async def test_cancel_github_failures(client, session, project, maintainer, github, response, status):
    build = await make_build(session, project, gh_run_id=RUN_ID, gh_run_attempt=1)
    await session.commit()
    if isinstance(response, Exception):
        github.post(CANCEL).mock(side_effect=response)
    else:
        github.post(CANCEL).mock(return_value=response)

    r = await client.post(f"/api/v1/builds/{build.id}/cancel")
    assert r.status_code == status
    (build,) = await _builds(session)
    assert build.status is S.queued
    assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0


async def test_cancel_unknown_build_is_404(client, maintainer, github):
    r = await client.post("/api/v1/builds/bld_NOPE/cancel")
    assert r.status_code == 404
