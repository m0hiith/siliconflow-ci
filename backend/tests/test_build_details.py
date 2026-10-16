"""What the build details page reads: GET /builds/{id} links and toolchain,
first-error source links, log search, gate results and the commit count to
the last passing build.

The end-to-end tests replay two recorded CI runs (tests/fixtures/ci_runs)
through the real pipeline: workflow_run webhooks, an OIDC-signed run report,
artifact ingestion, gate evaluation. Only GitHub's REST API is mocked. With
SFCI_UI_FIXTURES_DIR set they also write the API responses the page fetches,
as the frontend's Playwright fixtures:

    SFCI_UI_FIXTURES_DIR=/out pytest tests/test_build_details.py
"""
import io
import json
import os
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app.models import Build, BuildStatus, Repository
from app.services import links
from app.services.github import GitHubApp
from tests import oidc, reports
from tests.conftest import GITHUB_API
from tests.factories import make_build, make_project
from tests.github import deliver, drain, workflow_job, workflow_run

RUNS = Path(__file__).parent / "fixtures" / "ci_runs"
INSTALLATION = 42
AUTHOR = "m0hiith"
MESSAGES = {
    "passed": "Add hysteresis to threshold detector",
    "failed": "Expect a second rising edge in noisy-signal test",
}
# git rev-list --count <passed sha>..<failed sha> in the recorded history.
COMMITS_BETWEEN = 1


# ------------------------------------------------------------ pure links


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        ("/__w/fw/fw/firmware/tests/test_threshold.c", "firmware/tests/test_threshold.c"),
        ("/home/runner/work/fw/fw/firmware/src/main.c", "firmware/src/main.c"),
        ("/__w/fw/fw/firmware/src/a b.c", "firmware/src/a b.c"),
        # Another repository's workspace, a local capture, or relative paths: unknown.
        ("/__w/other/other/src/main.c", None),
        ("/w/firmware/src/sensor_logic.c", None),
        ("CMakeLists.txt", None),
        ("../firmware/src/main.c", None),
        ("/__w/fw/fw/../etc/passwd", None),
        ("/__w/fw/fw/", None),
    ],
)
def test_repo_relative(file, expected):
    assert links.repo_relative(file, "acme/fw") == expected


def test_links():
    web = "https://github.com/"
    assert links.commit_url(web, "acme/fw", None) is None
    assert links.commit_url(web, "acme/fw", "ab" * 20) == f"https://github.com/acme/fw/commit/{'ab' * 20}"
    assert links.run_url(web, "acme/fw", None, None) is None
    assert links.run_url(web, "acme/fw", 7, 2) == "https://github.com/acme/fw/actions/runs/7/attempts/2"
    assert (
        links.blob_url(web, "acme/fw", "c0ffee", "src/a b.c", 9)
        == "https://github.com/acme/fw/blob/c0ffee/src/a%20b.c#L9"
    )


# --------------------------------------------------------- unit endpoints


async def test_build_detail_without_run_report(client, session):
    project = await make_project(session, "fw")
    build = await make_build(session, project, gh_run_id=11, gh_run_attempt=2, commit_sha="a" * 40)
    await session.commit()

    body = (await client.get(f"/api/v1/builds/{build.id}")).json()
    assert body["repository"] == "acme/fw"
    assert body["commit_url"] == f"https://github.com/acme/fw/commit/{'a' * 40}"
    assert body["run_url"] == "https://github.com/acme/fw/actions/runs/11/attempts/2"
    assert body["toolchain"] == {}
    assert body["sdk"] == {}
    assert body["artifacts_state"] is None


async def test_gates_empty_and_404(client, session):
    project = await make_project(session)
    build = await make_build(session, project)
    await session.commit()
    assert (await client.get(f"/api/v1/builds/{build.id}/gates")).json() == []
    r = await client.get("/api/v1/builds/bld_01J9Z3K8Q4W5E6R7T8Y9U0I1O2/gates")
    assert r.status_code == 404


async def test_commits_between_needs_a_known_installation(client, session):
    """No installation on record: no GitHub call is made (respx would refuse it)."""
    project = await make_project(session)
    good = await make_build(session, project, commit_sha="a" * 40)
    good.status = BuildStatus.passed
    bad = await make_build(session, project, commit_sha="b" * 40)
    bad.status = BuildStatus.failed
    await session.commit()
    with respx.mock(assert_all_mocked=True):
        body = (await client.get(f"/api/v1/builds/{bad.id}/previous-passing")).json()
    assert body["build"]["id"] == good.id
    assert body["compare_url"] is not None and body["commits_between"] is None


@pytest.fixture
async def gh_client(settings):
    client = GitHubApp(settings)
    try:
        yield client
    finally:
        await client.aclose()


def _token_route(router):
    router.post(f"{GITHUB_API}/app/installations/{INSTALLATION}/access_tokens").respond(
        201, json={"token": "ghs_installation", "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}
    )


async def test_commits_ahead_is_cached_and_errors_raise(gh_client):
    with respx.mock(assert_all_called=False) as router:
        _token_route(router)
        ok = router.get(f"{GITHUB_API}/repos/acme/fw/compare/{'a' * 40}...{'b' * 40}").respond(
            200, json={"ahead_by": 3, "behind_by": 0}
        )
        router.get(f"{GITHUB_API}/repos/acme/fw/compare/{'a' * 40}...{'c' * 40}").respond(
            404, json={"message": "Not Found"}
        )
        assert await gh_client.commits_ahead(INSTALLATION, "acme/fw", "a" * 40, "b" * 40) == 3
        assert await gh_client.commits_ahead(INSTALLATION, "acme/fw", "a" * 40, "b" * 40) == 3
        assert ok.call_count == 1
        assert ok.calls[0].request.url.params["per_page"] == "1"
        with pytest.raises(Exception, match="compare: GitHub answered 404"):
            await gh_client.commits_ahead(INSTALLATION, "acme/fw", "a" * 40, "c" * 40)


# ------------------------------------------------- recorded runs, end to end


def _zip(out: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                zf.writestr(p.relative_to(out).as_posix(), p.read_bytes())
    return buf.getvalue()


def _ts(value: str, delta: float = 0) -> str:
    t = datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(seconds=delta)
    return t.isoformat().replace("+00:00", "Z")


@pytest.fixture(scope="module")
def key():
    return oidc.new_key()


@pytest.fixture
def github_mock(key):
    """GitHub for the recorded runs: artifacts, gates.yml and the compare API."""
    with respx.mock(assert_all_called=False) as router:
        router.get(oidc.JWKS_URL).respond(json=oidc.jwks((key, oidc.KID)))
        _token_route(router)
        for kind in ("passed", "failed"):
            report = json.loads((RUNS / kind / "report.json").read_text())
            run_id, name, zipped = report["github"]["run_id"], report["artifact_name"], _zip(RUNS / kind)
            blob = f"https://results.blob.example/{run_id}.zip"
            router.get(f"{GITHUB_API}/repos/acme/fw/actions/runs/{run_id}/artifacts").respond(
                200,
                json={"total_count": 1, "artifacts": [{
                    "id": run_id, "name": name, "size_in_bytes": len(zipped),
                    "expired": False, "workflow_run": {"id": run_id},
                }]},
            )
            router.get(f"{GITHUB_API}/repos/acme/fw/actions/artifacts/{run_id}/zip").respond(
                302, headers={"Location": blob}
            )
            router.get(blob).respond(200, content=zipped)
            router.get(
                f"{GITHUB_API}/repos/acme/fw/contents/.siliconflow/gates.yml",
                params={"ref": report["github"]["sha"]},
            ).respond(200, content=(RUNS / "gates.yml").read_bytes())
        passed_sha = json.loads((RUNS / "passed" / "report.json").read_text())["github"]["sha"]
        failed_sha = json.loads((RUNS / "failed" / "report.json").read_text())["github"]["sha"]
        router.get(f"{GITHUB_API}/repos/acme/fw/compare/{passed_sha}...{failed_sha}").respond(
            200, json={"status": "ahead", "ahead_by": COMMITS_BETWEEN, "behind_by": 0}
        )
        yield router


async def replay(client, sessionmaker, settings, github, key, repo: Repository, kind: str) -> str:
    """One recorded run, delivered the way GitHub and the runner deliver it."""
    report = json.loads((RUNS / kind / "report.json").read_text())
    gh = report["github"]
    stages = report["stages"]
    first_start = stages[0]["started_at"]
    last_end = max(s["finished_at"] for s in stages if s["finished_at"])

    def run_payload(action: str, **kw: Any) -> dict[str, Any]:
        payload = workflow_run(
            action, repo, run_id=gh["run_id"], display_title=MESSAGES[kind],
            started_at=_ts(first_start, -12), updated_at=_ts(last_end, 6), **kw,
        )
        run = payload["workflow_run"]
        run.update(head_sha=gh["sha"], head_branch="main", created_at=_ts(first_start, -15))
        run["head_commit"].update(id=gh["sha"], message=MESSAGES[kind])
        run["actor"]["login"] = run["triggering_actor"]["login"] = AUTHOR
        return payload

    assert (await deliver(client, "workflow_run", run_payload("requested"))).status_code == 202
    job = workflow_job(repo, run_id=gh["run_id"], started_at=_ts(first_start, -3))
    assert (await deliver(client, "workflow_job", job)).status_code == 202
    await drain(sessionmaker, settings, github)

    token = reports.token(key, run_id=gh["run_id"], sha=gh["sha"])
    r = await reports.post(client, report, token)
    assert r.status_code == 202, r.text
    await drain(sessionmaker, settings, github)

    conclusion = "success" if kind == "passed" else "failure"
    completed = run_payload("completed", conclusion=conclusion)
    assert (await deliver(client, "workflow_run", completed)).status_code == 202
    await drain(sessionmaker, settings, github)
    return r.json()["build_id"]


async def page_responses(client, build_id: str, searches: list[tuple[str, str]]) -> dict[str, Any]:
    """Every response the details page can fetch for a build, keyed by path."""
    out: dict[str, Any] = {}

    async def get(path: str, **params: Any) -> Any:
        r = await client.get(path, params=params)
        assert r.status_code == 200, (path, r.text)
        return r.json()

    base = f"/api/v1/builds/{build_id}"
    for path in ("", "/stages", "/tests", "/artifacts", "/gates", "/previous-passing", "/size"):
        out[base + path] = await get(base + path)
    out["/api/v1/projects"] = await get("/api/v1/projects")
    for stage in out[base + "/stages"]:
        r = await client.get(f"{base}/logs/{stage['name']}", params={"limit": 5000})
        out[f"{base}/logs/{stage['name']}"] = r.json() if r.status_code == 200 else {"status": r.status_code, **r.json()}
    for stage, q in searches:
        out[f"{base}/logs/{stage}/search?q={q}"] = await get(f"{base}/logs/{stage}/search", q=q)
    return out


def write_fixture(name: str, data: dict[str, Any]) -> None:
    target = os.environ.get("SFCI_UI_FIXTURES_DIR")
    if target:
        path = Path(target) / f"{name}.json"
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


async def test_recorded_runs_through_the_details_api(
    client, session, sessionmaker, settings, key, github_mock
):
    project = await make_project(session, "fw")
    repo = await session.get_one(Repository, project.repositories[0].id)
    repo.gh_installation_id = INSTALLATION
    await session.commit()

    github = GitHubApp(settings)
    try:
        passed_id = await replay(client, sessionmaker, settings, github, key, repo, "passed")
        failed_id = await replay(client, sessionmaker, settings, github, key, repo, "failed")
    finally:
        await github.aclose()

    session.expunge_all()
    passed = await session.get_one(Build, passed_id)
    failed = await session.get_one(Build, failed_id)
    assert (passed.status, failed.status) == (BuildStatus.passed, BuildStatus.failed)
    assert failed.failure_class == "test_failure"

    # Header facts.
    detail = (await client.get(f"/api/v1/builds/{failed_id}")).json()
    assert detail["toolchain"]["gcc"].endswith("13.3.0")
    assert detail["run_url"] == "https://github.com/acme/fw/actions/runs/30433701/attempts/1"
    assert detail["commit_message"] == MESSAGES["failed"]
    assert detail["author_login"] == AUTHOR

    # The first error points at the Unity assertion, in the repository at the commit.
    log = (await client.get(f"/api/v1/builds/{failed_id}/logs/test")).json()
    first = log["first_error"]
    assert first["path"] == "firmware/tests/test_threshold.c" and first["line"] == 75
    assert first["message"] == "test_noisy_signal_around_one_threshold: Expected 2 Was 1"
    assert first["url"] == (
        f"https://github.com/acme/fw/blob/{failed.commit_sha}/firmware/tests/test_threshold.c#L75"
    )

    # Search finds the failing assertion line, ignoring case.
    found = (await client.get(f"/api/v1/builds/{failed_id}/logs/test/search", params={"q": "fail:"})).json()
    assert found["matches"] == [first["line_number"]] and found["total"] == 1
    r = await client.get(f"/api/v1/builds/{failed_id}/logs/test/search", params={"q": ""})
    assert r.status_code == 422

    previous = (await client.get(f"/api/v1/builds/{failed_id}/previous-passing")).json()
    assert previous["build"]["id"] == passed_id
    assert previous["commits_between"] == COMMITS_BETWEEN

    tests = (await client.get(f"/api/v1/builds/{failed_id}/tests")).json()
    assert [t["name"] for t in tests if t["outcome"] == "failed"] == ["test_threshold"]

    # The report's job is the profile, so the host-cmake size limit applies.
    assert (passed.profile, failed.profile) == ("host-cmake", "host-cmake")
    gates = {g["rule_id"]: g for g in (await client.get(f"/api/v1/builds/{passed_id}/gates")).json()}
    assert all(g["passed"] for g in gates.values()), {k: g["message"] for k, g in gates.items() if not g["passed"]}
    assert gates["line_coverage_min"]["observed"] == 100.0
    assert gates["line_coverage_min"]["threshold"] == 80
    assert isinstance(gates["text_bytes_max"]["observed"], int)
    assert gates["text_bytes_max"]["observed"] <= gates["text_bytes_max"]["threshold"] == 40960
    artifacts = (await client.get(f"/api/v1/builds/{passed_id}/artifacts")).json()
    assert [a["release_eligible"] for a in artifacts] == [True, True]
    assert (await client.get(f"/api/v1/builds/{failed_id}/gates")).json() == []

    write_fixture("failed-build", {
        "build_id": failed_id,
        "responses": await page_responses(client, failed_id, [("test", "threshold"), ("test", "fail:")]),
    })
    write_fixture("passed-build", {
        "build_id": passed_id,
        "responses": await page_responses(client, passed_id, [("compile", "linking")]),
    })
