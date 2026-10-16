"""Linked run reports: the hardware-in-the-loop workflow adds a "hil" stage to
the siwx917-soc build it tested (services.linked_reports, docs/hil.md)."""
from datetime import datetime

import pytest
import respx
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config import Settings
from app.models import (
    AuditLog,
    Build,
    BuildStage,
    BuildStatus,
    BuildTrigger,
    Job,
    LinkedReport,
    StageStatus,
)
from app.schemas import ReportStage
from app.services import classify
from app.services.linked_reports import summarize
from tests import oidc, reports
from tests.factories import make_project
from tests.reports import make_run_build, post

S = BuildStatus
HIL_WORKFLOW = ".github/workflows/hil.yml"
HIL_RUN = 51000001
# A workflow_run token's sha is the default branch's head, not the tested commit.
HEAD_SHA = "9" * 40


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


async def siwx917_build(session, project, **fields) -> Build:
    """A siwx917-soc build whose own report has been accepted."""
    fields.setdefault("status", S.passed)
    build = await make_run_build(session, project, profile="siwx917-soc", **fields)
    for i, name in enumerate(("generate", "compile", "package")):
        session.add(BuildStage(build_id=build.id, name=name, position=i, status=StageStatus.passed, exit_code=0))
    await session.commit()
    return build


def stage(name, status="passed", exit_code=0, start="10:00:00", end="10:00:30"):
    return {
        "name": name,
        "status": status,
        "exit_code": exit_code,
        "started_at": f"2026-10-15T{start}Z" if start else None,
        "finished_at": f"2026-10-15T{end}Z" if end else None,
        "required": True,
    }


PASSED = [
    stage("fetch", start="10:00:00", end="10:00:20"),
    stage("flash", start="10:00:21", end="10:01:10"),
    stage("test", start="10:01:11", end="10:01:30"),
]


def hil_report(build_id, *, stages=PASSED, run_attempt=1, profile="siwx917-hil", **overrides):
    body = reports.report(
        run_id=HIL_RUN, run_attempt=run_attempt, sha=HEAD_SHA,
        artifact_name="sfci-out-siwx917-hil-attempt-1", manifest=None, junit=[],
        coverage=None, cppcheck=None, size=None, stages=stages,
        profile=profile, build_id=build_id, **overrides,
    )
    body["github"]["job"] = "siwx917-hil"
    return body


def hil_token(key, *, run_attempt=1, **claims):
    claims.setdefault("workflow_path", HIL_WORKFLOW)
    claims.setdefault("event_name", "workflow_run")
    return reports.token(key, run_id=HIL_RUN, run_attempt=run_attempt, sha=HEAD_SHA, **claims)


async def stages_of(client, build_id):
    r = await client.get(f"/api/v1/builds/{build_id}/stages")
    assert r.status_code == 200
    return r.json()


async def hil_stage(client, build_id):
    (hil,) = [s for s in await stages_of(client, build_id) if s["name"] == "hil"]
    return hil


# ------------------------------------------------------------- accepting


async def test_hil_report_adds_a_stage_to_the_build(client, session, mock, key, project):
    build = await siwx917_build(session, project)

    r = await post(client, hil_report(build.id), hil_token(key))
    assert r.status_code == 202, r.text
    assert r.json() == {"status": "accepted", "build_id": build.id}

    stages = await stages_of(client, build.id)
    assert [(s["name"], s["position"], s["required"], s["status"]) for s in stages] == [
        ("generate", 0, True, "passed"),
        ("compile", 1, True, "passed"),
        ("package", 2, True, "passed"),
        ("hil", 3, False, "passed"),
    ]
    hil = stages[3]
    assert hil["exit_code"] == 0 and hil["failure_class"] is None
    assert (hil["started_at"], hil["finished_at"]) == ("2026-10-15T10:00:00Z", "2026-10-15T10:01:30Z")

    linked = (await session.scalars(select(LinkedReport))).one()
    assert (linked.build_id, linked.profile, linked.stage) == (build.id, "siwx917-hil", "hil")
    assert (linked.gh_run_id, linked.gh_run_attempt) == (HIL_RUN, 1)
    assert linked.claims["workflow_ref"] == f"acme/fw/{HIL_WORKFLOW}@refs/heads/main"
    assert linked.payload["stages"][1]["name"] == "flash"

    session.expunge_all()
    after = await session.get_one(Build, build.id)
    assert after.status is S.passed and after.profile == "siwx917-soc"
    # Nothing to ingest or evaluate: the build's own report did that.
    assert await session.scalar(select(func.count()).select_from(Job)) == 0


async def test_infra_failure_is_classified_infra_hw(client, session, mock, key, project):
    build = await siwx917_build(session, project)
    stages = [PASSED[0], stage("flash", "failed", 75), stage("test", "skipped", None, None, None)]

    assert (await post(client, hil_report(build.id, stages=stages), hil_token(key))).status_code == 202
    hil = await hil_stage(client, build.id)
    assert (hil["status"], hil["exit_code"], hil["failure_class"]) == ("failed", 75, "infra_hw")
    # The build passed, and a hardware failure does not change that.
    session.expunge_all()
    assert (await session.get_one(Build, build.id)).status is S.passed


async def test_firmware_failure_is_not_infra(client, session, mock, key, project):
    build = await siwx917_build(session, project)
    stages = [PASSED[0], PASSED[1], stage("test", "failed", 1)]

    assert (await post(client, hil_report(build.id, stages=stages), hil_token(key))).status_code == 202
    hil = await hil_stage(client, build.id)
    assert (hil["status"], hil["exit_code"], hil["failure_class"]) == ("failed", 1, None)


async def test_identical_retry_is_a_duplicate(client, session, mock, key, project):
    build = await siwx917_build(session, project)
    body = hil_report(build.id)
    assert (await post(client, body, hil_token(key))).status_code == 202
    r = await post(client, body, hil_token(key))
    assert r.status_code == 200 and r.json() == {"status": "duplicate", "build_id": build.id}
    assert await session.scalar(select(func.count()).select_from(LinkedReport)) == 1


async def test_a_different_report_from_the_same_attempt_is_409(client, session, mock, key, project):
    build = await siwx917_build(session, project)
    assert (await post(client, hil_report(build.id), hil_token(key))).status_code == 202
    other = hil_report(build.id, stages=[PASSED[0], PASSED[1], stage("test", "failed", 1)])
    r = await post(client, other, hil_token(key))
    assert r.status_code == 409
    assert (await hil_stage(client, build.id))["status"] == "passed"


async def test_a_later_attempt_replaces_the_result(client, session, mock, key, project):
    build = await siwx917_build(session, project)
    infra = [PASSED[0], stage("flash", "failed", 75), stage("test", "skipped", None, None, None)]
    assert (await post(client, hil_report(build.id, stages=infra), hil_token(key))).status_code == 202

    r = await post(client, hil_report(build.id, run_attempt=2), hil_token(key, run_attempt=2))
    assert r.status_code == 202, r.text
    hil = await hil_stage(client, build.id)
    assert (hil["status"], hil["exit_code"], hil["failure_class"], hil["position"]) == ("passed", 0, None, 3)

    linked = (await session.scalars(select(LinkedReport))).one()
    assert linked.gh_run_attempt == 2
    audit = (await session.scalars(select(AuditLog))).one()
    assert (audit.action, audit.target_id) == ("linked_report.replaced", build.id)
    assert audit.details == {"profile": "siwx917-hil", "run_id": HIL_RUN, "from_attempt": 1, "to_attempt": 2}

    # The earlier attempt arriving late does not undo it.
    r = await post(client, hil_report(build.id, stages=infra), hil_token(key))
    assert r.status_code == 409
    assert (await hil_stage(client, build.id))["status"] == "passed"


# ------------------------------------------------------------- refusing


@pytest.mark.parametrize("status", [S.running, S.failed, S.errored])
async def test_only_passed_builds_are_tested(client, session, mock, key, project, status):
    build = await siwx917_build(session, project, status=status)
    r = await post(client, hil_report(build.id), hil_token(key))
    assert r.status_code == 409
    assert "not passed" in r.json()["detail"]
    assert await session.scalar(select(func.count()).select_from(LinkedReport)) == 0


async def test_unknown_build_is_404(client, session, mock, key, project):
    r = await post(client, hil_report("bld_01JA6Z9D5V8R3C2X1W0QZK7M4N"), hil_token(key))
    assert r.status_code == 404


async def test_other_profiles_builds_are_403(client, session, mock, key, project):
    build = await make_run_build(session, project, status=S.passed)  # host-cmake
    r = await post(client, hil_report(build.id), hil_token(key))
    assert r.status_code == 403
    assert r.json()["detail"] == "this profile tests siwx917-soc builds, not host-cmake"


async def test_pull_request_builds_are_403(client, session, mock, key, project):
    build = await siwx917_build(session, project, trigger=BuildTrigger.pull_request)
    r = await post(client, hil_report(build.id), hil_token(key))
    assert r.status_code == 403
    assert "pull request" in r.json()["detail"]


async def test_build_in_another_repository_is_403(client, session, mock, key, project):
    other = await make_project(session, "other")
    await session.commit()
    build = await siwx917_build(session, other)
    r = await post(client, hil_report(build.id), hil_token(key))
    assert r.status_code == 403
    assert r.json()["detail"] == "token repository does not match the build"


@pytest.mark.parametrize(
    "token_claims, detail",
    [
        # A firmware workflow cannot link to builds...
        ({"workflow_path": oidc.WORKFLOW_PATH, "event_name": "push"}, "token is not from a hardware-in-the-loop workflow"),
        # ...nor can an unlisted workflow.
        ({"workflow_path": ".github/workflows/other.yml"}, "token is not from a hardware-in-the-loop workflow"),
        ({"event_name": "workflow_dispatch"}, "hardware-in-the-loop reports must come from a workflow_run run"),
        ({"event_name": "pull_request"}, "hardware-in-the-loop reports must come from a workflow_run run"),
        # The workflow file as it is on another branch.
        ({"ref": "refs/heads/feature"}, "the workflow did not run from the default branch"),
    ],
)
async def test_tokens_that_may_not_link_are_403(client, session, mock, key, project, token_claims, detail):
    build = await siwx917_build(session, project)
    r = await post(client, hil_report(build.id), hil_token(key, **token_claims))
    assert r.status_code == 403
    assert r.json()["detail"] == detail


async def test_unknown_profile_is_403(client, session, mock, key, project):
    build = await siwx917_build(session, project)
    r = await post(client, hil_report(build.id, profile="cortex-m4-hil"), hil_token(key))
    assert r.status_code == 403
    assert r.json()["detail"] == "this workflow does not report profile 'cortex-m4-hil'"


async def test_report_must_describe_the_tokens_run(client, session, mock, key, project):
    build = await siwx917_build(session, project)
    body = hil_report(build.id)
    body["github"]["run_id"] = HIL_RUN + 1
    r = await post(client, body, hil_token(key))
    assert r.status_code == 403


async def test_hil_workflow_cannot_post_a_build_report(client, session, mock, key, project):
    await make_run_build(session, project, run_id=HIL_RUN)
    body = reports.report(run_id=HIL_RUN, sha=HEAD_SHA, artifact_name=None, manifest=None, junit=[])
    r = await post(client, body, hil_token(key))
    assert r.status_code == 403
    assert r.json()["detail"] == "token is not from a firmware workflow"


@pytest.mark.parametrize("fields", [{"profile": "siwx917-hil"}, {"build_id": "bld_01JA6Z9D5V8R3C2X1W0QZK7M4N"}])
async def test_profile_and_build_id_go_together(client, session, mock, key, project, fields):
    body = reports.report(run_id=HIL_RUN, sha=HEAD_SHA, artifact_name=None, manifest=None, junit=[], **fields)
    r = await post(client, body, hil_token(key))
    assert r.status_code == 422


async def test_a_build_with_its_own_hil_stage_is_409(client, session, mock, key, project):
    build = await siwx917_build(session, project)
    session.add(BuildStage(build_id=build.id, name="hil", position=3, status=StageStatus.passed))
    await session.commit()
    r = await post(client, hil_report(build.id), hil_token(key))
    assert r.status_code == 409
    assert r.json()["detail"] == "build already has a hil stage"


# --------------------------------------------------------------- finding


async def test_builds_can_be_found_by_run_attempt(client, session, project):
    first = await make_run_build(session, project, run_id=700, attempt=1)
    second = await make_run_build(session, project, run_id=700, attempt=2)
    await make_run_build(session, project, run_id=701, attempt=1)

    r = await client.get("/api/v1/builds", params={"gh_run_id": 700, "gh_run_attempt": 2})
    assert [b["id"] for b in r.json()["items"]] == [second.id]
    r = await client.get("/api/v1/builds", params={"gh_run_id": 700})
    assert sorted(b["id"] for b in r.json()["items"]) == sorted([first.id, second.id])
    assert (await client.get("/api/v1/builds", params={"gh_run_id": 0})).status_code == 422


# ------------------------------------------------------------ pure parts


def _stage(name, status="passed", exit_code=0, start=None, end=None):
    at = lambda t: datetime.fromisoformat(f"2026-10-15T{t}+00:00") if t else None  # noqa: E731
    return ReportStage(name=name, status=status, exit_code=exit_code, started_at=at(start), finished_at=at(end))


def test_summary_of_passed_stages():
    s = summarize([_stage("fetch", start="10:00:00", end="10:00:05"), _stage("flash", end="10:01:00"), _stage("test")])
    assert (s.status, s.exit_code) == (StageStatus.passed, 0)
    assert (s.started_at.isoformat(), s.finished_at.isoformat()) == (
        "2026-10-15T10:00:00+00:00",
        "2026-10-15T10:01:00+00:00",
    )


def test_summary_takes_the_first_unpassed_stages_exit_code():
    s = summarize([_stage("fetch"), _stage("flash", "failed", 75), _stage("test", "skipped", None)])
    assert (s.status, s.exit_code) == (StageStatus.failed, 75)


def test_summary_fails_when_a_hil_stage_never_ran():
    s = summarize([_stage("fetch"), _stage("flash")])
    assert (s.status, s.exit_code) == (StageStatus.failed, None)
    assert summarize([]).status is StageStatus.failed


@pytest.mark.parametrize("code, expected", [(75, "infra_hw"), (1, None), (0, None), (None, None), (2, None)])
def test_exit_code_classification(code, expected):
    index = classify.exit_code_index(code)
    assert (index.failure_class if index else None) == expected
    if index:
        assert index.rule == "exit-code-75" and index.first_error is None


def test_hil_workflow_cannot_also_be_a_firmware_workflow(database_url):
    with pytest.raises(ValidationError, match="both a firmware and a hardware-in-the-loop"):
        Settings(
            database_url=database_url,
            github_hil_workflows={".github/workflows/firmware-ci.yml": {"x-hil": "host-cmake"}},
        )
    with pytest.raises(ValidationError, match="valid report and build profiles"):
        Settings(database_url=database_url, github_hil_workflows={HIL_WORKFLOW: {}})
    settings = Settings(database_url=database_url)
    assert settings.hil_profiles_for(HIL_WORKFLOW) == {"siwx917-hil": "siwx917-soc"}
    assert settings.required_stages_for(HIL_WORKFLOW) is None
