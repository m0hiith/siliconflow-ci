from datetime import datetime, timedelta, timezone

from app.models import Artifact, BuildStage, BuildStatus, StageStatus, TestCase, TestOutcome
from app.services.transitions import transition
from tests.factories import make_build, make_project

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- health


async def test_health_and_readiness(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"

    r = await client.get("/api/v1/health/ready")
    assert r.status_code == 200
    assert r.json()["detail"]["schema_version"] == "0005"


async def test_request_id_is_echoed(client):
    r = await client.get("/api/v1/health", headers={"x-request-id": "abc123"})
    assert r.headers["x-request-id"] == "abc123"


# -------------------------------------------------------------- projects


async def test_projects_empty(client):
    r = await client.get("/api/v1/projects")
    assert r.status_code == 200
    assert r.json() == []


async def test_projects_list_and_get(client, session):
    await make_project(session, "zeta")
    alpha = await make_project(session, "alpha")
    await session.commit()

    r = await client.get("/api/v1/projects")
    body = r.json()
    assert [p["slug"] for p in body] == ["alpha", "zeta"]
    assert body[0]["repositories"][0]["full_name"] == "acme/alpha"

    r = await client.get(f"/api/v1/projects/{alpha.id}")
    assert r.status_code == 200 and r.json()["slug"] == "alpha"

    r = await client.get("/api/v1/projects/prj_NOPE")
    assert r.status_code == 404


# ---------------------------------------------------------------- builds


async def _seed_builds(session):
    """7 builds with distinct created_at, oldest first."""
    project = await make_project(session, "fw")
    other = await make_project(session, "other")
    specs = [
        (project, "main", "host-cmake", BuildStatus.passed),
        (project, "main", "cortex-m4", BuildStatus.failed),
        (project, "develop", "host-cmake", BuildStatus.passed),
        (project, "main", "host-cmake", BuildStatus.errored),
        (other, "main", "host-cmake", BuildStatus.passed),
        (project, "feature/x", "host-cmake", BuildStatus.running),
        (project, "main", "host-cmake", BuildStatus.queued),
    ]
    builds = []
    for i, (proj, branch, profile, status) in enumerate(specs):
        b = await make_build(
            session, proj, branch=branch, profile=profile, created_at=T0 + timedelta(minutes=i)
        )
        b.status = status
        builds.append(b)
    await session.commit()
    return project, other, builds


async def test_builds_newest_first(client, session):
    _, _, builds = await _seed_builds(session)
    r = await client.get("/api/v1/builds")
    assert r.status_code == 200
    body = r.json()
    assert [b["id"] for b in body["items"]] == [b.id for b in reversed(builds)]
    assert body["next_cursor"] is None


async def test_builds_filters(client, session):
    project, other, builds = await _seed_builds(session)

    async def ids(**params):
        r = await client.get("/api/v1/builds", params=params)
        assert r.status_code == 200, r.text
        return {b["id"] for b in r.json()["items"]}

    assert await ids(status="failed") == {builds[1].id}
    assert await ids(status=["failed", "errored"]) == {builds[1].id, builds[3].id}
    assert await ids(branch="develop") == {builds[2].id}
    assert await ids(profile="cortex-m4") == {builds[1].id}
    assert await ids(project_id=other.id) == {builds[4].id}
    assert await ids(project_id=project.id, branch="main", status="passed", profile="host-cmake") == {
        builds[0].id
    }
    assert await ids(branch="no-such-branch") == set()


async def test_builds_invalid_status_is_422(client):
    r = await client.get("/api/v1/builds", params={"status": "exploded"})
    assert r.status_code == 422


async def test_builds_cursor_pagination_walks_every_row_once(client, session):
    _, _, builds = await _seed_builds(session)
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/api/v1/builds", params=params)
        assert r.status_code == 200
        body = r.json()
        seen += [b["id"] for b in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == [b.id for b in reversed(builds)]


async def test_builds_pagination_ties_on_created_at_broken_by_id(client, session):
    project = await make_project(session)
    same = [await make_build(session, project, created_at=T0) for _ in range(5)]
    await session.commit()

    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = (await client.get("/api/v1/builds", params=params)).json()
        seen += [b["id"] for b in body["items"]]
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(b.id for b in same)
    assert len(seen) == len(set(seen)) == 5


async def test_builds_pagination_stable_when_new_builds_arrive(client, session):
    project, _, builds = await _seed_builds(session)
    first = (await client.get("/api/v1/builds", params={"limit": 3})).json()

    await make_build(session, project, created_at=T0 + timedelta(hours=1))
    await session.commit()

    second = (
        await client.get("/api/v1/builds", params={"limit": 3, "cursor": first["next_cursor"]})
    ).json()
    assert [b["id"] for b in second["items"]] == [b.id for b in reversed(builds)][3:6]


async def test_builds_bad_cursor_is_400(client):
    for bad in ["not-base64!!", "eyJ4IjoxfQ", ""]:
        r = await client.get("/api/v1/builds", params={"cursor": bad})
        assert r.status_code == 400, bad


async def test_builds_limit_bounds(client):
    assert (await client.get("/api/v1/builds", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/v1/builds", params={"limit": 201})).status_code == 422


async def test_build_detail_and_404(client, session):
    project = await make_project(session)
    build = await make_build(
        session, project, commit_sha="0" * 40, gh_run_id=99, gh_run_attempt=1
    )
    await session.commit()

    r = await client.get(f"/api/v1/builds/{build.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["number"] == 1
    assert body["status"] == "queued"
    assert body["commit_sha"] == "0" * 40
    assert body["gh_run_id"] == 99

    for suffix in ("", "/stages", "/tests", "/artifacts"):
        r = await client.get(f"/api/v1/builds/bld_NOPE{suffix}")
        assert r.status_code == 404, suffix


async def test_build_stages_ordered_by_position(client, session):
    project = await make_project(session)
    build = await make_build(session, project)
    for pos, name, status in [
        (3, "analyze", StageStatus.skipped),
        (1, "configure", StageStatus.passed),
        (2, "test", StageStatus.failed),
    ]:
        session.add(BuildStage(build_id=build.id, name=name, position=pos, status=status))
    await session.commit()

    r = await client.get(f"/api/v1/builds/{build.id}/stages")
    assert [(s["name"], s["status"]) for s in r.json()] == [
        ("configure", "passed"),
        ("test", "failed"),
        ("analyze", "skipped"),
    ]


async def test_build_tests_with_outcome_filter(client, session):
    project = await make_project(session)
    build = await make_build(session, project)
    session.add_all(
        [
            TestCase(build_id=build.id, suite="debounce", name="t_ok", outcome=TestOutcome.passed),
            TestCase(
                build_id=build.id,
                suite="debounce",
                name="t_bad",
                outcome=TestOutcome.failed,
                message="Expected 1 Was 0",
            ),
            TestCase(build_id=build.id, suite="mavg", name="t_skip", outcome=TestOutcome.skipped),
        ]
    )
    await session.commit()

    r = await client.get(f"/api/v1/builds/{build.id}/tests")
    assert [t["name"] for t in r.json()] == ["t_bad", "t_ok", "t_skip"]

    r = await client.get(f"/api/v1/builds/{build.id}/tests", params={"outcome": "failed"})
    assert [(t["name"], t["message"]) for t in r.json()] == [("t_bad", "Expected 1 Was 0")]


async def test_build_artifacts(client, session):
    project = await make_project(session)
    build = await make_build(session, project)
    session.add(
        Artifact(build_id=build.id, kind="elf", file_name="firmware", size_bytes=96976, sha256="b" * 64)
    )
    await session.commit()

    r = await client.get(f"/api/v1/builds/{build.id}/artifacts")
    (art,) = r.json()
    assert art["id"].startswith("art_")
    assert art["sha256"] == "b" * 64
    assert art["size_bytes"] == 96976
    assert art["release_eligible"] is False


# ------------------------------------------------------- previous passing


def _sha(n: int) -> str:
    return f"{n:040x}"


async def _finished(session, project, status: BuildStatus, n: int, **fields):
    fields.setdefault("commit_sha", _sha(n))
    build = await make_build(session, project, **fields)
    await transition(session, build.id, BuildStatus.running, T0)
    await transition(session, build.id, status, T0 + timedelta(minutes=5))
    return build


async def test_previous_passing_build_and_compare_url(client, session):
    project = await make_project(session, "fw")
    older = await _finished(session, project, BuildStatus.passed, 1)
    good = await _finished(session, project, BuildStatus.passed, 2)
    await _finished(session, project, BuildStatus.failed, 3)
    # Passing builds that are not comparable: other ref, other profile, later.
    await _finished(session, project, BuildStatus.passed, 4, branch="develop")
    await _finished(session, project, BuildStatus.passed, 5, profile="efr32-gcc")
    broken = await _finished(session, project, BuildStatus.failed, 6)
    await _finished(session, project, BuildStatus.passed, 7)
    await session.commit()

    r = await client.get(f"/api/v1/builds/{broken.id}/previous-passing")
    assert r.status_code == 200
    body = r.json()
    assert body["build"]["id"] == good.id and body["build"]["status"] == "passed"
    assert body["compare_url"] == f"https://github.com/acme/fw/compare/{_sha(2)}...{_sha(6)}"

    # The passing build itself looks further back.
    body = (await client.get(f"/api/v1/builds/{good.id}/previous-passing")).json()
    assert body["build"]["id"] == older.id


async def test_previous_passing_none(client, session):
    project = await make_project(session)
    first = await _finished(session, project, BuildStatus.failed, 1)
    await session.commit()
    r = await client.get(f"/api/v1/builds/{first.id}/previous-passing")
    assert r.status_code == 200
    assert r.json() == {"build": None, "compare_url": None, "commits_between": None}


async def test_previous_passing_compare_url_needs_two_different_commits(client, session):
    project = await make_project(session)
    await _finished(session, project, BuildStatus.passed, 1)
    rerun = await _finished(session, project, BuildStatus.failed, 2, commit_sha=_sha(1))
    queued = await make_build(session, project)  # no commit yet
    await session.commit()

    for build in (rerun, queued):
        body = (await client.get(f"/api/v1/builds/{build.id}/previous-passing")).json()
        assert body["build"]["commit_sha"] == _sha(1)
        assert body["compare_url"] is None


async def test_previous_passing_unknown_build_is_404(client):
    r = await client.get("/api/v1/builds/bld_01J9Z3K8Q4W5E6R7T8Y9U0I1O2/previous-passing")
    assert r.status_code == 404
