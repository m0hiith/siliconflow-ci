"""Artifact ingestion (download, re-hash, content-addressed storage, JUnit)
and GET /api/v1/artifacts/{id}/download."""
import hashlib
import io
import json
import stat
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.models import Artifact, AuditLog, Job, JobStatus, Repository, RunReport, TestCase
from app.services.artifacts import (
    Expected,
    IntegrityError,
    kind_of,
    storage_key,
    storage_path,
    verify_and_store,
)
from app.services.github import GitHubApp
from tests import oidc, reports
from tests.conftest import GITHUB_API
from tests.factories import make_project
from tests.github import drain, make_retries_ready
from tests.reports import ARTIFACT_NAME, OUT_DIR, RUN_ID, make_run_build, post

INSTALLATION = 42
ARTIFACT_ID = 5550001
BLOB_URL = "https://productionresultssa0.blob.core.windows.net/actions-results/abc/out.zip?sig=s3cr3t"
FIRMWARE = (OUT_DIR / "artifacts" / "firmware").read_bytes()
FIRMWARE_BIN = (OUT_DIR / "artifacts" / "firmware.bin").read_bytes()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(scope="module")
def key():
    return oidc.new_key()


class FakeGitHub:
    """GitHub's artifact API and blob storage, mocked with respx."""

    def __init__(self, router: respx.Router) -> None:
        self.zip = reports.artifact_zip()
        self.listed = True
        self.expired = False
        router.post(f"{GITHUB_API}/app/installations/{INSTALLATION}/access_tokens").respond(
            201,
            json={
                "token": "ghs_installation",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        self.listing = router.get(f"{GITHUB_API}/repos/acme/fw/actions/runs/{RUN_ID}/artifacts").mock(
            side_effect=self._list
        )
        self.redirect = router.get(
            f"{GITHUB_API}/repos/acme/fw/actions/artifacts/{ARTIFACT_ID}/zip"
        ).respond(302, headers={"Location": BLOB_URL})
        self.blob = router.get(BLOB_URL).mock(side_effect=lambda request: httpx.Response(200, content=self.zip))

    def _list(self, request: httpx.Request) -> httpx.Response:
        assert request.url.params["name"] == ARTIFACT_NAME
        artifacts = [
            {"id": 1, "name": "something-else", "workflow_run": {"id": RUN_ID}},
            {
                "id": ARTIFACT_ID,
                "name": ARTIFACT_NAME,
                "size_in_bytes": len(self.zip),
                "expired": self.expired,
                "workflow_run": {"id": RUN_ID},
            },
        ]
        if not self.listed:
            artifacts = []
        return httpx.Response(200, json={"total_count": len(artifacts), "artifacts": artifacts})


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
async def build(session, project):
    return await make_run_build(session, project)


async def report_and_ingest(client, session, sessionmaker, settings, github, key, body=None):
    r = await post(client, body or reports.report(), reports.token(key))
    assert r.status_code == 202, r.text
    await drain(sessionmaker, settings, github)
    session.expunge_all()
    return (await session.scalars(select(RunReport))).one()


async def _count(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


def stored_files(settings) -> list[Path]:
    root = Path(settings.artifact_root) / "sha256"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


# ------------------------------------------------------------- ingestion


async def test_artifacts_are_verified_stored_and_downloadable(
    client, session, sessionmaker, settings, github, gh, key, build
):
    report = await report_and_ingest(client, session, sessionmaker, settings, github, key)
    assert (report.artifacts_state, report.artifacts_detail) == ("stored", None)

    rows = {a.file_name: a for a in (await session.scalars(select(Artifact))).all()}
    assert set(rows) == {"firmware", "firmware.bin"}
    assert (rows["firmware"].kind, rows["firmware.bin"].kind) == ("file", "bin")
    for name, data in (("firmware", FIRMWARE), ("firmware.bin", FIRMWARE_BIN)):
        art = rows[name]
        assert art.id.startswith("art_") and art.build_id == build.id
        assert (art.size_bytes, art.sha256) == (len(data), sha(data))
        assert art.storage_key == f"sha256/{sha(data)[:2]}/{sha(data)[2:4]}/{sha(data)}"
        path = Path(settings.artifact_root, art.storage_key)
        assert path.read_bytes() == data
        assert not path.stat().st_mode & stat.S_IWUSR  # stored read-only

    # Nothing left behind in the staging area.
    assert list((Path(settings.artifact_root) / "tmp").iterdir()) == []

    cases = (await session.scalars(select(TestCase).order_by(TestCase.name))).all()
    assert [(c.suite, c.name, c.outcome.value, c.duration_ms) for c in cases] == [
        ("Linux-c", "test_debounce", "passed", 4),
        ("Linux-c", "test_moving_average", "passed", 4),
        ("Linux-c", "test_threshold", "passed", 4),
    ]

    # The installation token went to api.github.com and never to blob storage.
    assert gh.listing.calls.last.request.headers["authorization"] == "Bearer ghs_installation"
    assert "authorization" not in gh.blob.calls.last.request.headers

    listed = (await client.get(f"/api/v1/builds/{build.id}/artifacts")).json()
    assert {a["file_name"] for a in listed} == {"firmware", "firmware.bin"}
    r = await client.get(f"/api/v1/artifacts/{rows['firmware.bin'].id}/download")
    assert r.status_code == 200
    assert r.content == FIRMWARE_BIN
    assert r.headers["x-checksum-sha256"] == sha(FIRMWARE_BIN)
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == 'attachment; filename="firmware.bin"'
    assert int(r.headers["content-length"]) == len(FIRMWARE_BIN)


@pytest.mark.parametrize(
    ("zip_overrides", "problem"),
    [
        ({"artifacts/firmware.bin": b"BIN:evil\n"}, "artifacts/firmware.bin: manifest says 13 bytes"),
        ({"artifacts/firmware.bin": FIRMWARE_BIN[:-1] + b"?"}, "artifacts/firmware.bin: manifest says 13 bytes"),
        ({"artifacts/firmware": None}, "artifacts/firmware: missing from the artifact"),
        ({"artifacts/backdoor.bin": b"x"}, "artifacts/backdoor.bin: not in the manifest"),
        ({"junit/ctest.xml": b"<testsuite name='forged'/>"}, "junit/ctest.xml: size or sha256 differs"),
    ],
)
async def test_any_mismatch_rejects_everything(
    client, session, sessionmaker, settings, github, gh, key, build, zip_overrides, problem
):
    gh.zip = reports.artifact_zip(zip_overrides)
    report = await report_and_ingest(client, session, sessionmaker, settings, github, key)

    assert report.artifacts_state == "rejected"
    assert problem in report.artifacts_detail
    assert await _count(session, Artifact) == 0
    assert await _count(session, TestCase) == 0
    assert stored_files(settings) == []
    audit = (await session.scalars(select(AuditLog))).one()
    assert (audit.action, audit.target_id) == ("artifacts.rejected", build.id)
    assert problem in audit.details["detail"]
    # Rejection is a recorded outcome, not a job to retry.
    assert (await session.scalars(select(Job))).one().status is JobStatus.done


async def test_manifest_in_the_zip_is_not_trusted(client, session, sessionmaker, settings, github, gh, key, build):
    # Rewriting the zip's own manifest.json to match tampered content changes nothing:
    # the reference is the manifest in the authenticated report.
    evil = b"BIN:evil\n"
    manifest = json.loads((OUT_DIR / "artifacts" / "manifest.json").read_text())
    for f in manifest["files"]:
        if f["file"] == "firmware.bin":
            f.update(size=len(evil), sha256=sha(evil))
    gh.zip = reports.artifact_zip(
        {"artifacts/firmware.bin": evil, "artifacts/manifest.json": json.dumps(manifest).encode()}
    )
    report = await report_and_ingest(client, session, sessionmaker, settings, github, key)
    assert report.artifacts_state == "rejected"


async def test_artifact_not_listed_yet_is_retried(client, session, sessionmaker, settings, github, gh, key, build):
    gh.listed = False
    await report_and_ingest(client, session, sessionmaker, settings, github, key)
    job = (await session.scalars(select(Job))).one()
    assert (job.status, job.attempts) == (JobStatus.queued, 1)
    assert job.last_error.startswith("ArtifactNotReady: artifact 'sfci-out-host-cmake-attempt-1' not listed")

    gh.listed = True
    await make_retries_ready(sessionmaker)
    await drain(sessionmaker, settings, github)
    session.expunge_all()
    assert (await session.scalars(select(RunReport))).one().artifacts_state == "stored"
    assert await _count(session, Artifact) == 2


async def test_expired_artifact_is_unavailable(client, session, sessionmaker, settings, github, gh, key, build):
    gh.expired = True
    report = await report_and_ingest(client, session, sessionmaker, settings, github, key)
    assert (report.artifacts_state, report.artifacts_detail) == (
        "unavailable",
        "artifact 'sfci-out-host-cmake-attempt-1' has expired",
    )
    assert not gh.redirect.called


async def test_gone_zip_is_unavailable(client, session, sessionmaker, settings, github, gh, key, build):
    gh.redirect.respond(410, json={"message": "Artifact has expired"})
    report = await report_and_ingest(client, session, sessionmaker, settings, github, key)
    assert report.artifacts_state == "unavailable"
    assert "410" in report.artifacts_detail


async def test_oversized_artifact_is_not_downloaded(
    client, session, sessionmaker, settings, github, gh, key, build
):
    settings.artifact_max_download_bytes = 100
    report = await report_and_ingest(client, session, sessionmaker, settings, github, key)
    assert report.artifacts_state == "unavailable"
    assert "over the 100-byte limit" in report.artifacts_detail
    assert not gh.blob.called


async def test_report_without_manifest_still_reads_junit(
    client, session, sessionmaker, settings, github, gh, key, build
):
    # e.g. the package stage failed: no manifest, but the tests ran.
    report = await report_and_ingest(
        client, session, sessionmaker, settings, github, key, reports.report(manifest=None)
    )
    assert report.artifacts_state == "stored"
    assert await _count(session, Artifact) == 0
    assert await _count(session, TestCase) == 3


async def test_identical_content_is_stored_once(client, session, sessionmaker, settings, github, gh, key, project, build):
    await report_and_ingest(client, session, sessionmaker, settings, github, key)
    # A re-run (attempt 2) produces byte-identical firmware.
    await make_run_build(session, project, attempt=2)
    r = await post(client, reports.report(run_attempt=2), reports.token(key, run_attempt=2))
    assert r.status_code == 202
    await drain(sessionmaker, settings, github)

    rows = (await session.scalars(select(Artifact))).all()
    assert len(rows) == 4 and len({a.build_id for a in rows}) == 2
    assert len({a.storage_key for a in rows}) == 2
    assert len(stored_files(settings)) == 2


# -------------------------------------------------------------- download


async def test_download_of_unknown_artifact_is_404(client):
    r = await client.get("/api/v1/artifacts/art_01J9Z3K8Q4W5E6R7T8Y9U0I1O2/download")
    assert r.status_code == 404 and r.json()["detail"] == "artifact not found"


async def test_download_without_stored_content_is_404(client, session, settings, build):
    data = b"never stored"
    art = Artifact(build_id=build.id, kind="bin", file_name="x.bin", size_bytes=len(data), sha256=sha(data))
    session.add(art)
    await session.commit()
    r = await client.get(f"/api/v1/artifacts/{art.id}/download")
    assert r.status_code == 404 and r.json()["detail"] == "artifact content is not stored"

    # A row whose content vanished from the store is not served either.
    art.storage_key = storage_key(sha(data))
    await session.commit()
    r = await client.get(f"/api/v1/artifacts/{art.id}/download")
    assert r.status_code == 404


# ------------------------------------------------------ verify_and_store


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def expect(name: str, data: bytes) -> Expected:
    return Expected(f"artifacts/{name}", len(data), sha(data))


def test_longer_file_is_caught_without_reading_it_all(tmp_path):
    big = b"x" * 100_000
    z = make_zip(tmp_path / "a.zip", {"artifacts/fw.bin": big})
    with pytest.raises(IntegrityError) as exc:
        verify_and_store(z, tmp_path / "store", [Expected("artifacts/fw.bin", 10, sha(big[:10]))], [])
    assert "got 11+ bytes" in str(exc.value)  # stopped one byte past the claim
    assert not (tmp_path / "store" / "sha256").exists()


def test_not_a_zip_is_rejected(tmp_path):
    bad = tmp_path / "a.zip"
    bad.write_bytes(b"PK\x03\x04 truncated")
    with pytest.raises(IntegrityError, match="not a valid zip"):
        verify_and_store(bad, tmp_path / "store", [], [])


def test_without_a_manifest_extra_files_are_ignored(tmp_path):
    z = make_zip(tmp_path / "a.zip", {"artifacts/half-packaged.bin": b"x"})
    result = verify_and_store(z, tmp_path / "store", None, [])
    assert result.artifacts == []


def test_zip_entry_names_cannot_escape_the_store(tmp_path):
    # Files are stored by hash, never by their name in the zip.
    data = b"payload"
    z = make_zip(tmp_path / "a.zip", {"artifacts/../../escape.bin": data})
    result = verify_and_store(z, tmp_path / "store", [Expected("artifacts/../../escape.bin", 7, sha(data))], [])
    assert [p.name for p in (tmp_path / "store").rglob("*") if p.is_file()] == [sha(data)]
    assert result.artifacts[0][0] == "../../escape.bin"
    assert not (tmp_path / "escape.bin").exists()


def test_storage_key_layout_and_validation(tmp_path):
    digest = sha(b"abc")
    assert storage_key(digest) == f"sha256/ba/78/{digest}"
    assert storage_path(tmp_path, storage_key(digest)) == tmp_path / "sha256" / "ba" / "78" / digest
    for bad in ("../../etc/passwd", "ABC", digest.upper()):
        with pytest.raises(ValueError):
            storage_key(bad)
    for bad in (f"sha256/00/78/{digest}", f"sha256/ba/78/../{digest}", "sha256/ba/78", f"md5/ba/78/{digest}"):
        with pytest.raises(ValueError):
            storage_path(tmp_path, bad)


@pytest.mark.parametrize(
    ("name", "head", "kind"),
    [
        ("firmware.elf", b"", "elf"),
        ("FIRMWARE.BIN", b"", "bin"),
        ("app.hex", b"", "hex"),
        ("app.map", b"", "map"),
        ("siwx917_soc.rps", b"", "rps"),
        ("siwx917_soc.s37", b"", "s37"),
        ("siwx917_soc_isp.bin", b"", "bin"),
        ("siwx917_soc.out", b"\x7fELF\x01\x01", "elf"),
        ("firmware", b"\x7fELF\x02\x01", "elf"),
        ("firmware", b"\xcf\xfa\xed\xfe", "file"),
        ("notes.txt", b"plain text", "file"),
    ],
)
def test_kind_of(name, head, kind):
    assert kind_of(name, head) == kind


def test_zip_fixture_matches_the_report_fixture():
    """Guard for the test data itself: the zip must be what complete.json describes."""
    body = reports.report()
    with zipfile.ZipFile(io.BytesIO(reports.artifact_zip())) as zf:
        for f in body["manifest"]["files"]:
            assert sha(zf.read(f"artifacts/{f['file']}")) == f["sha256"]
        for j in body["junit"]:
            assert sha(zf.read(j["path"])) == j["sha256"]
