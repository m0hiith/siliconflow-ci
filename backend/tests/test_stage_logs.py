"""Stage logs: capping, redaction, storage during artifact ingestion, and
GET /api/v1/builds/{id}/logs/{stage}."""
import gzip
import io
import stat
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Build, BuildStage
from app.services import stage_logs
from app.services.artifacts import storage_path
from tests import oidc, reports

# The ingestion harness (GitHub mocks, project and build) is shared with test_artifacts.
from tests.test_artifacts import (  # noqa: F401  (pytest fixtures)
    build,
    gh,
    github,
    key,
    mock,
    project,
    report_and_ingest,
    stored_files,
)

LOGS = Path(__file__).parent / "fixtures" / "logs"
GHS = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # an installation-token-shaped string
GHP = "ghp_" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp9Oo8"


def log(name: str) -> bytes:
    return (LOGS / name).read_bytes()


def jwt() -> str:
    """A real RS256 JWT, as GitHub's OIDC endpoint issues them."""
    return oidc.sign(oidc.claims(run_id=1, run_attempt=1, sha="0" * 40), oidc.new_key())


# -------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    "secret",
    [
        GHS,
        GHP,
        "gho_" + "a" * 36,
        "ghu_" + "b" * 36,
        "ghr_" + "c" * 76,
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUV",
    ],
)
def test_github_tokens_are_redacted(secret):
    for line in (
        f"token: {secret}",
        f"git clone https://x-access-token:{secret}@github.com/acme/fw.git",
        f"GITHUB_TOKEN_{secret}",  # "_" counts as a boundary
        f'{{"token":"{secret}"}}',
    ):
        out = stage_logs.redact(line)
        assert secret not in out and secret[4:] not in out, out
        assert "[REDACTED:github-token]" in out


def test_jwts_are_redacted():
    token = jwt()
    out = stage_logs.redact(f"Authorization: Bearer {token}\nnext line")
    assert out == "Authorization: Bearer [REDACTED:jwt]\nnext line"
    # Unsigned tokens ("alg": "none") have an empty signature.
    header, payload, _ = token.split(".")
    assert stage_logs.redact(f"{header}.{payload}.") == "[REDACTED:jwt]"


@pytest.mark.parametrize(
    "text",
    [
        "ghp_short",  # too short to be a token
        "sha256:3484a3fb816e0859fd6e1cea078d76385ff50625",
        "eyJustAWord and eyJ.eyJ.x",
        "aghs_" + "x" * 36,  # part of a longer word
    ],
)
def test_ordinary_text_is_not_redacted(text):
    assert stage_logs.redact(text) == text


# ------------------------------------------------------------------ capping


def test_short_log_is_read_whole():
    data = log("gcc-undeclared.compile.log")
    assert stage_logs.read_capped(io.BytesIO(data), len(data)) == (data, 0)


def test_long_log_keeps_whole_lines_from_head_and_tail():
    lines = [f"line {n:05d} {'x' * 40}".encode() for n in range(1, 20001)]
    data = b"\n".join(lines) + b"\n"
    limit = 64 * 1024
    out, omitted = stage_logs.read_capped(io.BytesIO(data), limit)

    head, marker, tail = out.partition(f"[siliconflow: {omitted} bytes omitted; log over {limit} bytes]\n".encode())
    assert marker, out[:200]
    assert len(head) + len(tail) + omitted == len(data)
    assert len(head) <= limit // 2 and len(tail) <= limit // 2
    assert data.startswith(head) and data.endswith(tail)
    # cut at line ends only
    assert head.endswith(b"\n") and tail.startswith(b"line ")
    assert tail.endswith(b"line 20000 " + b"x" * 40 + b"\n")


def test_split_lines():
    assert stage_logs.split_lines("") == []
    assert stage_logs.split_lines("a\nb\n") == ["a", "b"]
    assert stage_logs.split_lines("a\r\nb") == ["a", "b"]
    assert stage_logs.split_lines("a\n\nb\n") == ["a", "", "b"]
    assert stage_logs.split_lines("progress\r50%\n") == ["progress\r50%"]


def test_prepare_redacts_before_indexing_and_compresses_deterministically():
    data = log("gcc-undeclared.compile.log") + f"curl -H 'Authorization: token {GHS}'\n".encode()
    prepared = stage_logs.prepare(io.BytesIO(data), 1 << 20)
    text = gzip.decompress(prepared.gz).decode()
    assert GHS not in text and "[REDACTED:github-token]" in text
    assert prepared.index.failure_class == "compile_error"
    assert prepared.index.line_count == 15
    assert stage_logs.prepare(io.BytesIO(data), 1 << 20).gz == prepared.gz


@pytest.mark.parametrize(
    ("entry", "stage"),
    [
        ("logs/compile.log", "compile"),
        ("logs/unit-tests.v2.log", "unit-tests.v2"),
        ("logs/sub/compile.log", None),
        ("logs/.log", None),
        ("logs/compile.txt", None),
        ("artifacts/logs/compile.log", None),
        ("compile.log", None),
    ],
)
def test_stage_of(entry, stage):
    assert stage_logs.stage_of(entry) == stage


# ---------------------------------------------------------------- ingestion


def failed_compile_report() -> dict:
    """compile failed, so nothing was packaged and no test ran."""
    body = reports.report(manifest=None, junit=[])
    reports.with_stage(body, "compile", "failed", 1)
    for name in ("test", "analyze", "package"):
        reports.with_stage(body, name, "skipped", None)
    return body


async def _stage(session, build_id: str, name: str) -> BuildStage:
    return await session.scalar(
        select(BuildStage).where(BuildStage.build_id == build_id, BuildStage.name == name)
    )


async def test_logs_are_redacted_classified_stored_and_served(
    client, session, sessionmaker, settings, github, gh, key, build
):
    token = jwt()
    compile_log = log("gcc-undeclared.compile.log") + (
        f"+ curl -H 'Authorization: Bearer {token}' https://api.github.com\n"
        f"+ git push https://x-access-token:{GHS}@github.com/acme/fw.git\n"
    ).encode()
    gh.zip = reports.artifact_zip(
        {
            "logs/configure.log": log("clean.configure.log"),
            "logs/compile.log": compile_log,
            "logs/lint.log": b"no such stage\n",  # not a stage of this build
            "logs/sub/compile.log": b"not a stage log\n",
        }
    )
    # No manifest and no JUnit: the zip is still fetched for its logs.
    report = await report_and_ingest(
        client, session, sessionmaker, settings, github, key, failed_compile_report()
    )
    assert (report.artifacts_state, report.artifacts_detail) == ("stored", None)

    compile_stage = await _stage(session, build.id, "compile")
    configure_stage = await _stage(session, build.id, "configure")
    assert compile_stage.log_key and configure_stage.log_key
    assert (await _stage(session, build.id, "test")).log_key is None
    assert compile_stage.log_index["failure_class"] == "compile_error"
    assert configure_stage.log_index["failure_class"] is None
    assert (await session.get(Build, build.id)).failure_class == "compile_error"

    # Stored gzipped, read-only, content-addressed, with no token left in it.
    path = storage_path(settings.artifact_root, compile_stage.log_key)
    assert len(stored_files(settings)) == 2
    assert not path.stat().st_mode & stat.S_IWUSR
    text = gzip.decompress(path.read_bytes()).decode()
    assert token not in text and GHS not in text
    assert text.startswith(log("gcc-undeclared.compile.log").decode())

    r = await client.get(f"/api/v1/builds/{build.id}/logs/compile")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stage"] == "compile" and body["offset"] == 0
    assert body["total_lines"] == 16 and len(body["lines"]) == 16
    assert body["lines"][4].startswith("/w/firmware/src/sensor_logic.c:74:20: error:")
    assert body["lines"][14] == "+ curl -H 'Authorization: Bearer [REDACTED:jwt]' https://api.github.com"
    assert body["lines"][15] == (
        "+ git push https://x-access-token:[REDACTED:github-token]@github.com/acme/fw.git"
    )
    assert (body["errors"], body["warnings"]) == ([2, 5], [])
    assert (body["error_count"], body["warning_count"]) == (2, 0)
    assert body["failure_class"] == "compile_error"
    assert body["first_error"] == {
        "line_number": 5,
        "message": "'SL_MAVG_LIMIT' undeclared (first use in this function)",
        "file": "/w/firmware/src/sensor_logic.c",
        "line": 74,
        "column": 20,
        # Captured outside an Actions workspace, so not mapped to a repository path.
        "path": None,
        "url": None,
    }

    # Paging: to show error line 5, skip 4 lines.
    page = (await client.get(f"/api/v1/builds/{build.id}/logs/compile", params={"offset": 4, "limit": 2})).json()
    assert page["lines"] == body["lines"][4:6]
    assert (page["offset"], page["total_lines"], page["errors"]) == (4, 16, [2, 5])
    past_end = (await client.get(f"/api/v1/builds/{build.id}/logs/compile", params={"offset": 100})).json()
    assert past_end["lines"] == [] and past_end["total_lines"] == 16


async def test_log_endpoint_errors(client, session, sessionmaker, settings, github, gh, key, build):
    gh.zip = reports.artifact_zip({"logs/compile.log": log("gcc-undeclared.compile.log")})
    await report_and_ingest(client, session, sessionmaker, settings, github, key, failed_compile_report())

    async def get(path, **params):
        return await client.get(f"/api/v1/builds/{path}", params=params)

    r = await get("bld_01J9Z3K8Q4W5E6R7T8Y9U0I1O2/logs/compile")
    assert (r.status_code, r.json()["detail"]) == (404, "build not found")
    r = await get(f"{build.id}/logs/flash")
    assert (r.status_code, r.json()["detail"]) == (404, "stage not found")
    r = await get(f"{build.id}/logs/test")  # skipped: no log
    assert (r.status_code, r.json()["detail"]) == (404, "no log stored for this stage")
    for params in ({"offset": -1}, {"limit": 0}, {"limit": 5001}):
        assert (await get(f"{build.id}/logs/compile", **params)).status_code == 422

    # Content lost from the store: 404, not 500.
    stage = await _stage(session, build.id, "compile")
    path = storage_path(settings.artifact_root, stage.log_key)
    path.chmod(0o644)
    path.unlink()
    r = await get(f"{build.id}/logs/compile")
    assert (r.status_code, r.json()["detail"]) == (404, "no log stored for this stage")


async def test_rejected_zip_stores_no_logs(client, session, sessionmaker, settings, github, gh, key, build):
    gh.zip = reports.artifact_zip(
        {"logs/compile.log": log("gcc-undeclared.compile.log"), "artifacts/firmware.bin": b"BIN:evil\n"}
    )
    report = await report_and_ingest(client, session, sessionmaker, settings, github, key)
    assert report.artifacts_state == "rejected"
    assert (await _stage(session, build.id, "compile")).log_key is None
    assert (await session.get(Build, build.id)).failure_class is None
    assert stored_files(settings) == []


async def test_failure_class_is_from_the_first_failed_required_stage(
    client, session, sessionmaker, settings, github, gh, key, build
):
    body = reports.report(manifest=None, junit=[])
    reports.with_stage(body, "test", "failed", 8)
    reports.with_stage(body, "analyze", "failed", 1)
    reports.with_stage(body, "package", "skipped", None)
    # An optional stage that failed earlier does not decide the class.
    body["stages"].insert(
        0, {"name": "lint", "status": "failed", "exit_code": 1, "required": False}
    )
    gh.zip = reports.artifact_zip(
        {
            "logs/lint.log": log("ld-undefined-reference.compile.log"),
            "logs/compile.log": log("clean.compile.log"),
            "logs/test.log": log("ctest-unity-fail.test.log"),
            "logs/analyze.log": log("cppcheck-null-pointer.analyze.log"),
        }
    )
    await report_and_ingest(client, session, sessionmaker, settings, github, key, body)

    assert (await session.get(Build, build.id)).failure_class == "test_failure"
    classes = {
        s.name: s.log_index["failure_class"]
        for s in (await session.scalars(select(BuildStage).where(BuildStage.build_id == build.id))).all()
        if s.log_index
    }
    assert classes == {
        "lint": "link_error",
        "compile": None,
        "test": "test_failure",
        "analyze": "analysis",
    }


async def test_unclassified_failure_leaves_failure_class_unset(
    client, session, sessionmaker, settings, github, gh, key, build
):
    gh.zip = reports.artifact_zip({"logs/compile.log": b"[1/15] Building C object x.o\nKilled\n"})
    await report_and_ingest(client, session, sessionmaker, settings, github, key, failed_compile_report())
    assert (await _stage(session, build.id, "compile")).log_key is not None
    assert (await session.get(Build, build.id)).failure_class is None


async def test_oversized_log_is_capped(client, session, sessionmaker, settings, github, gh, key, build):
    settings.stage_log_max_bytes = 4096
    noise = b"".join(f"[{n}/9999] Building C object obj{n}.o\n".encode() for n in range(2000))
    gh.zip = reports.artifact_zip({"logs/compile.log": noise + log("gcc-undeclared.compile.log")})
    await report_and_ingest(client, session, sessionmaker, settings, github, key, failed_compile_report())

    body = (await client.get(f"/api/v1/builds/{build.id}/logs/compile", params={"limit": 5000})).json()
    assert any(line.startswith("[siliconflow: ") for line in body["lines"])
    assert sum(len(line) + 1 for line in body["lines"]) < 4096 + 100
    # The error at the end survives, and is still what classifies the log.
    assert body["failure_class"] == "compile_error"
    assert body["first_error"]["message"] == "'SL_MAVG_LIMIT' undeclared (first use in this function)"
    assert body["lines"][body["first_error"]["line_number"] - 1].startswith("/w/firmware/src/sensor_logic.c:74:20")
