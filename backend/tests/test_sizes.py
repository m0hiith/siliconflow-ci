"""ELF sizes: the cortex-m4 workflow's run reports, build_sizes, the size
endpoints, and the extra-workflow settings they rely on."""
import pytest
import respx
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.models import Build, BuildSize, BuildStage, BuildStatus
from tests import oidc, reports
from tests.factories import make_project
from tests.github import deliver, drain, workflow_run
from tests.reports import RUN_ID, make_run_build, post
from tests.test_api import _finished

M4_WORKFLOW = ".github/workflows/firmware-ci-cortex-m4.yml"
# ci/size.py output for the cortex-m4 build (arm-none-eabi-size, MinSizeRel).
SIZE = {
    "file": "firmware.elf",
    "text_bytes": 7472,
    "data_bytes": 96,
    "bss_bytes": 8484,
    "sections": [
        {"name": ".isr_vector", "size": 64, "addr": 0},
        {"name": ".text", "size": 7400, "addr": 64},
        {"name": ".data", "size": 96, "addr": 536870912},
    ],
}


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


def m4_report(**overrides):
    """A cortex-m4 job's report: no tests, a size stage and a size."""
    body = reports.report(artifact_name=None, manifest=None, junit=[], coverage=None, cppcheck=None)
    body["github"]["job"] = "cortex-m4"
    body["stages"] = [
        {"name": name, "status": "passed", "exit_code": 0, "required": True}
        for name in ("configure", "compile", "package", "size")
    ]
    body["size"] = SIZE
    body.update(overrides)
    return body


def m4_token(key):
    return reports.token(key, workflow_path=M4_WORKFLOW)


# ------------------------------------------------------------ run reports


async def test_cortex_m4_report_is_stored_with_its_size(client, session, mock, key, project):
    build = await make_run_build(session, project, profile="firmware-ci-cortex-m4")
    r = await post(client, m4_report(), m4_token(key))
    assert r.status_code == 202, r.text

    size = await session.get_one(BuildSize, build.id)
    assert (size.file_name, size.text_bytes, size.data_bytes, size.bss_bytes) == ("firmware.elf", 7472, 96, 8484)
    assert size.sections == SIZE["sections"]
    stages = (await session.scalars(select(BuildStage).order_by(BuildStage.position))).all()
    # The cortex-m4 workflow's required stages apply: no test stage is added.
    assert [(s.name, s.required, s.status.value) for s in stages] == [
        ("configure", True, "passed"),
        ("compile", True, "passed"),
        ("package", True, "passed"),
        ("size", True, "passed"),
    ]
    session.expunge_all()
    assert (await session.get_one(Build, build.id)).profile == "cortex-m4"


async def test_cortex_m4_required_stages_cannot_be_dropped(client, session, mock, key, project):
    await make_run_build(session, project)
    body = m4_report()
    body["stages"] = body["stages"][:3]  # the size stage never ran
    assert (await post(client, body, m4_token(key))).status_code == 202
    size_stage = await session.scalar(select(BuildStage).where(BuildStage.name == "size"))
    assert (size_stage.required, size_stage.status.value) == (True, "skipped")


async def test_host_workflow_still_requires_its_own_stages(client, session, mock, key, project):
    await make_run_build(session, project)
    # A host-cmake token with the cortex-m4 stage list: test and analyze are still required.
    assert (await post(client, m4_report(), reports.token(key))).status_code == 202
    names = set(
        (await session.scalars(select(BuildStage.name).where(BuildStage.status == "skipped"))).all()
    )
    assert names == {"test", "analyze"}


async def test_a_report_without_size_stores_none(client, session, mock, key, project):
    await make_run_build(session, project)
    assert (await post(client, m4_report(size=None), m4_token(key))).status_code == 202
    assert (await session.scalars(select(BuildSize))).all() == []


@pytest.mark.parametrize(
    "size",
    [
        {**SIZE, "text_bytes": -1},
        {**SIZE, "text_bytes": "7472"},
        {**SIZE, "file": "../firmware.elf"},
        {**SIZE, "surprise": 1},
        {k: v for k, v in SIZE.items() if k != "bss_bytes"},
        {**SIZE, "sections": [{"name": ".text", "size": 1}]},
    ],
)
async def test_malformed_size_is_422(client, session, mock, key, project, size):
    await make_run_build(session, project)
    assert (await post(client, m4_report(size=size), m4_token(key))).status_code == 422


async def test_cortex_m4_workflow_runs_create_builds(client, session, sessionmaker, settings, project):
    repo = project.repositories[0]
    payload = workflow_run("requested", repo, run_id=RUN_ID, path=M4_WORKFLOW)
    assert (await deliver(client, "workflow_run", payload)).status_code == 202
    await drain(sessionmaker, settings)
    build = await session.scalar(select(Build).where(Build.gh_run_id == RUN_ID))
    assert build is not None and build.profile == "firmware-ci-cortex-m4"


# --------------------------------------------------------------- settings


def test_extra_workflows_come_from_json_env(monkeypatch, database_url):
    monkeypatch.setenv("SFCI_GITHUB_EXTRA_WORKFLOWS", '{".github/workflows/m33.yml": ["configure", "compile"]}')
    settings = Settings(database_url=database_url)
    assert settings.required_stages_for(".github/workflows/m33.yml") == ("configure", "compile")
    assert settings.required_stages_for(M4_WORKFLOW) is None  # replaced, not merged
    assert settings.required_stages_for(".github/workflows/firmware-ci.yml") == settings.required_stages
    assert settings.required_stages_for(".github/workflows/toolchain-image.yml") is None


def test_default_workflows_include_cortex_m4(database_url):
    settings = Settings(database_url=database_url)
    assert settings.required_stages_for(M4_WORKFLOW.upper().replace(".GITHUB/WORKFLOWS", ".github/workflows")) == (
        "configure", "compile", "package", "size",
    )


def test_default_workflows_include_siwx917_soc(database_url):
    settings = Settings(database_url=database_url)
    assert settings.required_stages_for(".github/workflows/firmware-ci-siwx917-soc.yml") == (
        "generate", "compile", "package",
    )


@pytest.mark.parametrize(
    "value",
    [
        {"firmware-ci-m4.yml": ["compile"]},  # not under .github/workflows
        {M4_WORKFLOW: []},
        {M4_WORKFLOW: ["bad stage"]},
    ],
)
def test_invalid_extra_workflows_are_refused(database_url, value):
    with pytest.raises(ValidationError):
        Settings(database_url=database_url, github_extra_workflows=value)


# -------------------------------------------------------------------- API


async def _with_size(session, build, text: int, data: int = 8, bss: int = 64, sections=None):
    session.add(
        BuildSize(
            build_id=build.id, file_name="firmware.elf", text_bytes=text, data_bytes=data,
            bss_bytes=bss, sections=sections or [{"name": ".text", "size": text, "addr": 0}],
        )
    )
    await session.flush()
    return build


async def test_project_sizes_newest_first_with_filters(client, session, project):
    S = BuildStatus
    a = await _with_size(session, await _finished(session, project, S.passed, 1, profile="cortex-m4"), 1000)
    b = await _with_size(session, await _finished(session, project, S.failed, 2, profile="cortex-m4", branch="dev"), 1100)
    c = await _with_size(session, await _finished(session, project, S.passed, 3, profile="host-cmake"), 30000)
    await _finished(session, project, S.passed, 4, profile="cortex-m4")  # no size reported
    await session.commit()

    async def ids(**params):
        r = await client.get(f"/api/v1/projects/{project.id}/sizes", params=params)
        assert r.status_code == 200, r.text
        return [p["build_id"] for p in r.json()]

    assert await ids() == [c.id, b.id, a.id]
    assert await ids(branch="main") == [c.id, a.id]
    assert await ids(profile="cortex-m4") == [b.id, a.id]
    assert await ids(branch="main", profile="cortex-m4", limit=1) == [a.id]

    (point,) = (await client.get(f"/api/v1/projects/{project.id}/sizes", params={"branch": "dev"})).json()
    assert point == {
        "build_id": b.id, "build_number": b.number, "status": "failed", "profile": "cortex-m4",
        "branch": "dev", "commit_sha": f"{2:040x}", "created_at": point["created_at"],
        "file_name": "firmware.elf", "text_bytes": 1100, "data_bytes": 8, "bss_bytes": 64,
    }


async def test_project_sizes_unknown_project_is_404(client):
    assert (await client.get("/api/v1/projects/prj_NOPE/sizes")).status_code == 404


async def test_build_size_is_compared_with_the_latest_passing_base_build(client, session, project):
    S = BuildStatus
    m4 = {"profile": "cortex-m4"}
    await _with_size(session, await _finished(session, project, S.passed, 1, **m4), 900)
    base = await _with_size(session, await _finished(session, project, S.passed, 2, **m4), 1000)
    # Not the base: failed, another profile, another branch, or no size.
    await _with_size(session, await _finished(session, project, S.failed, 3, **m4), 1200)
    await _with_size(session, await _finished(session, project, S.passed, 4, profile="host-cmake"), 30000)
    await _with_size(session, await _finished(session, project, S.passed, 5, branch="dev", **m4), 1300)
    await _finished(session, project, S.passed, 6, **m4)
    feature = await _with_size(session, await _finished(session, project, S.passed, 7, branch="feature", **m4), 1100)
    # Later main builds are not "before" the feature build.
    await _with_size(session, await _finished(session, project, S.passed, 8, **m4), 5000)
    await session.commit()

    r = await client.get(f"/api/v1/builds/{feature.id}/size")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["base_branch"] == "main"
    assert body["base_build"]["id"] == base.id
    assert (body["size"]["text_bytes"], body["base_size"]["text_bytes"]) == (1100, 1000)
    assert body["size"]["sections"] == [{"name": ".text", "size": 1100, "addr": 0}]


async def test_build_size_without_size_or_base(client, session, project):
    build = await _finished(session, project, BuildStatus.failed, 1, profile="cortex-m4")
    await session.commit()
    r = await client.get(f"/api/v1/builds/{build.id}/size")
    assert r.status_code == 200
    assert r.json() == {"size": None, "base_branch": "main", "base_build": None, "base_size": None}
    assert (await client.get("/api/v1/builds/bld_NOPE/size")).status_code == 404
