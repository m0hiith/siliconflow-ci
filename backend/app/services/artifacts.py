"""Artifact ingestion and content-addressed storage.

When a run report arrives, a worker job (INGEST_JOB_KIND) downloads the run's
Actions artifact (the job's out/ directory, zipped) through the GitHub API and
checks it against the report:

  - every file in the report's manifest must be in the zip under artifacts/,
    with the same size and SHA-256, and there must be no unlisted files there;
  - every JUnit file the report lists must match its size and SHA-256.

Any mismatch rejects the whole set: nothing is stored, the run report's
artifacts_state becomes "rejected" and an audit_log row records the problems.
Otherwise each artifact is stored once, by content, at

    <artifact_root>/sha256/ab/cd/<sha256>

and the JUnit files are parsed into test_cases. The hashes the server computes
are the ones recorded; the manifest is only a claim to check them against.

If everything matches, each stage's log (logs/<stage>.log) is redacted,
classified and stored gzipped the same way; see services.stage_logs.
"""
import asyncio
import hashlib
import logging
import os
import re
import shutil
import uuid
import zipfile
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.ids import new_id
from app.models import Artifact, AuditLog, Build, BuildStage, Repository, RunReport, TestCase
from app.services import stage_logs
from app.services.classify import LogIndex
from app.services.github import GitHubApp, GitHubError
from app.services.junit import JUnitError, parse_junit

log = logging.getLogger(__name__)

INGEST_JOB_KIND = "ingest_artifacts"
ARTIFACTS_PREFIX = "artifacts/"
MANIFEST_NAME = "manifest.json"
CHUNK = 1 << 16
MAX_DETAIL_LEN = 2000
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

KIND_BY_SUFFIX = {
    ".elf": "elf",
    ".bin": "bin",
    ".hex": "hex",
    ".map": "map",
    ".uf2": "uf2",
    ".s37": "s37",
    # SiWx91x flash image (Simplicity Commander's `rps create`).
    ".rps": "rps",
}
ELF_MAGIC = b"\x7fELF"


class ArtifactNotReady(Exception):
    """The run's artifact is not listed yet; the worker retries with backoff."""


class IntegrityError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Expected:
    """A file the report claims is in the zip."""

    entry: str  # path inside the zip
    size: int
    sha256: str


@dataclass
class Verified:
    artifacts: list[tuple[str, int, str, str]] = field(default_factory=list)  # name, size, sha, kind
    junit: dict[str, bytes] = field(default_factory=dict)
    logs: dict[str, tuple[str, LogIndex]] = field(default_factory=dict)  # stage -> storage key, index


# ------------------------------------------------------------------ storage


def storage_key(sha256: str) -> str:
    if not SHA256_RE.match(sha256):
        raise ValueError("not a SHA-256 hex digest")
    return f"sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}"


def storage_path(root: str | Path, key: str) -> Path:
    parts = key.split("/")
    if len(parts) != 4 or parts[0] != "sha256" or storage_key(parts[3]) != key:
        raise ValueError(f"not a storage key: {key!r}")
    return Path(root, *parts)


def kind_of(name: str, head: bytes) -> str:
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in KIND_BY_SUFFIX:
        return KIND_BY_SUFFIX[suffix]
    return "elf" if head.startswith(ELF_MAGIC) else "file"


def _copy_hashing(src: IO[bytes], dst: IO[bytes] | None, limit: int) -> tuple[int, str, bytes]:
    """Copy at most limit+1 bytes, returning (size, sha256, first bytes)."""
    h = hashlib.sha256()
    size, head = 0, b""
    while chunk := src.read(min(CHUNK, limit + 1 - size)):
        if not head:
            head = chunk[:16]
        h.update(chunk)
        size += len(chunk)
        if dst is not None:
            dst.write(chunk)
        if size > limit:
            break
    return size, h.hexdigest(), head


def verify_and_store(
    zip_path: Path,
    root: Path,
    artifacts: list[Expected] | None,
    junit: list[Expected],
    log_stages: Collection[str] = (),
    log_max_bytes: int = 16 << 20,
) -> Verified:
    """Check the zip against the report and store the artifacts, and the logs
    of `log_stages`. Blocking: run it in a thread. Raises IntegrityError,
    having stored nothing, on any mismatch. `artifacts` is None when the
    report had no manifest."""
    staging = root / "tmp" / uuid.uuid4().hex
    staging.mkdir(parents=True)
    problems: list[str] = []
    result = Verified()
    staged: list[tuple[Path, str]] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            entries = {info.filename: info for info in zf.infolist() if not info.is_dir()}

            if artifacts is not None:
                listed = {e.entry for e in artifacts}
                for name in sorted(entries):
                    if (
                        name.startswith(ARTIFACTS_PREFIX)
                        and name != ARTIFACTS_PREFIX + MANIFEST_NAME
                        and name not in listed
                    ):
                        problems.append(f"{name}: not in the manifest")

            for expected in artifacts or []:
                if expected.entry not in entries:
                    problems.append(f"{expected.entry}: missing from the artifact")
                    continue
                tmp = staging / str(len(staged))
                with zf.open(expected.entry) as src, tmp.open("wb") as dst:
                    size, sha, head = _copy_hashing(src, dst, expected.size)
                if (size, sha) != (expected.size, expected.sha256):
                    problems.append(
                        f"{expected.entry}: manifest says {expected.size} bytes sha256 "
                        f"{expected.sha256}, got {size}{'+' if size > expected.size else ''} "
                        f"bytes sha256 {sha}"
                    )
                    continue
                name = expected.entry.removeprefix(ARTIFACTS_PREFIX)
                staged.append((tmp, sha))
                result.artifacts.append((name, size, sha, kind_of(name, head)))

            for expected in junit:
                if expected.entry not in entries:
                    problems.append(f"{expected.entry}: missing from the artifact")
                    continue
                with zf.open(expected.entry) as src:
                    data = src.read(expected.size + 1)
                sha = hashlib.sha256(data).hexdigest()
                if (len(data), sha) != (expected.size, expected.sha256):
                    problems.append(f"{expected.entry}: size or sha256 differs from the report")
                    continue
                result.junit[expected.entry] = data

            # Logs are not in the report, so there is nothing to check them
            # against; they are only kept when everything else matched.
            if not problems:
                for entry in sorted(entries):
                    stage = stage_logs.stage_of(entry)
                    if stage is None or stage not in log_stages:
                        continue
                    with zf.open(entry) as src:
                        prepared = stage_logs.prepare(src, log_max_bytes)
                    sha = hashlib.sha256(prepared.gz).hexdigest()
                    tmp = staging / str(len(staged))
                    tmp.write_bytes(prepared.gz)
                    staged.append((tmp, sha))
                    result.logs[stage] = (storage_key(sha), prepared.index)
    except zipfile.BadZipFile as exc:
        problems.append(f"not a valid zip: {exc}")

    try:
        if problems:
            raise IntegrityError(problems)
        for tmp, sha in staged:
            final = storage_path(root, storage_key(sha))
            if final.exists():
                continue  # the same content is already stored
            final.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(tmp, 0o444)
            os.replace(tmp, final)
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# -------------------------------------------------------------------- job


async def repo_installation_id(session: AsyncSession, github: GitHubApp, repo: Repository) -> int:
    if repo.gh_installation_id is None:
        repo.gh_installation_id = await github.repository_installation_id(repo.full_name)
        await session.flush()
    return repo.gh_installation_id


async def _finish(
    session: AsyncSession, report: RunReport, state: str, detail: str | None, build_id: str
) -> None:
    report.artifacts_state = state
    report.artifacts_detail = detail[:MAX_DETAIL_LEN] if detail else None
    if state in ("rejected", "unavailable"):
        session.add(
            AuditLog(
                action=f"artifacts.{state}",
                target_type="build",
                target_id=build_id,
                details={"detail": report.artifacts_detail},
            )
        )
        log.warning(f"artifacts {state}", extra={"build_id": build_id, "detail": report.artifacts_detail})
    await session.flush()


async def ingest(session: AsyncSession, github: GitHubApp, settings: Settings, build_id: str) -> None:
    report = await session.scalar(
        select(RunReport).where(RunReport.build_id == build_id).with_for_update()
    )
    if report is None or report.artifacts_state != "pending":
        return  # already done, or nothing to do
    build = await session.get_one(Build, build_id)
    repo = await session.get_one(Repository, build.repository_id)
    payload = report.payload
    name = payload["artifact_name"]

    installation_id = await repo_installation_id(session, github, repo)
    artifact = await github.find_run_artifact(installation_id, repo.full_name, build.gh_run_id, name)
    if artifact is None:
        raise ArtifactNotReady(f"artifact {name!r} not listed for run {build.gh_run_id}")
    if artifact.get("expired"):
        return await _finish(session, report, "unavailable", f"artifact {name!r} has expired", build_id)
    if artifact.get("size_in_bytes", 0) > settings.artifact_max_download_bytes:
        return await _finish(
            session, report, "unavailable",
            f"artifact {name!r} is {artifact['size_in_bytes']} bytes, over the "
            f"{settings.artifact_max_download_bytes}-byte limit", build_id,
        )

    root = Path(settings.artifact_root)
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    zip_path = root / "tmp" / f"{uuid.uuid4().hex}.zip"
    try:
        try:
            await github.download_artifact(
                installation_id, repo.full_name, artifact["id"], zip_path,
                settings.artifact_max_download_bytes,
            )
        except GitHubError as exc:
            if exc.status == 410:
                return await _finish(session, report, "unavailable", str(exc), build_id)
            raise
        manifest = payload.get("manifest")
        expected_artifacts = (
            [Expected(ARTIFACTS_PREFIX + f["file"], f["size"], f["sha256"]) for f in manifest["files"]]
            if manifest is not None
            else None
        )
        expected_junit = [Expected(j["path"], j["size"], j["sha256"]) for j in payload.get("junit", [])]
        stages = set(await session.scalars(select(BuildStage.name).where(BuildStage.build_id == build_id)))
        try:
            verified = await asyncio.to_thread(
                verify_and_store, zip_path, root, expected_artifacts, expected_junit,
                stages, settings.stage_log_max_bytes,
            )
        except IntegrityError as exc:
            return await _finish(session, report, "rejected", str(exc), build_id)
    finally:
        zip_path.unlink(missing_ok=True)

    for file_name, size, sha, kind in verified.artifacts:
        await session.execute(
            pg_insert(Artifact)
            .values(
                id=new_id("art"),
                build_id=build_id,
                kind=kind,
                file_name=file_name,
                size_bytes=size,
                sha256=sha,
                storage_key=storage_key(sha),
            )
            .on_conflict_do_nothing(constraint="uq_artifacts_build_id_kind_sha256")
        )

    cases = {}
    junit_problems = []
    for path, data in verified.junit.items():
        try:
            for case in parse_junit(data):
                cases[(case.suite, case.name)] = case  # a later duplicate wins
        except JUnitError as exc:
            junit_problems.append(f"{path}: {exc}")
    if cases:
        await session.execute(
            pg_insert(TestCase)
            .values(
                [
                    {
                        "build_id": build_id,
                        "suite": c.suite,
                        "name": c.name,
                        "classname": c.classname,
                        "outcome": c.outcome,
                        "duration_ms": c.duration_ms,
                        "message": c.message,
                    }
                    for c in cases.values()
                ]
            )
            .on_conflict_do_nothing(constraint="uq_test_cases_build_id_suite_name")
        )

    failure_class = await stage_logs.record(session, build_id, verified.logs)

    # artifacts_detail is about JUnit only: the test-results gate reads it.
    detail = "; ".join(junit_problems) or None
    await _finish(session, report, "stored", detail, build_id)
    log.info(
        "artifacts stored",
        extra={
            "build_id": build_id,
            "artifacts": len(verified.artifacts),
            "test_cases": len(cases),
            "logs": len(verified.logs),
            "failure_class": failure_class,
        },
    )
