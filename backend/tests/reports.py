"""Run report test helpers.

complete.json is real ci/report.py output for the "complete" CI fixture, and
artifact-zip/ holds the out/ files it describes, so these tests exercise the
contract between the runner and the backend, not a hand-written imitation.
"""
import copy
import io
import json
import zipfile
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Build, BuildStatus, BuildTrigger, Project
from app.services.transitions import transition
from tests import oidc
from tests.factories import make_build
from tests.github import SHA

FIXTURES = Path(__file__).parent / "fixtures" / "run_report"
OUT_DIR = FIXTURES / "artifact-zip"
RUN_ID = 30433642
ARTIFACT_NAME = "sfci-out-host-cmake-attempt-1"


@cache
def _complete() -> dict[str, Any]:
    return json.loads((FIXTURES / "complete.json").read_text())


def report(*, run_id: int = RUN_ID, run_attempt: int = 1, sha: str = SHA, **overrides: Any) -> dict[str, Any]:
    body = copy.deepcopy(_complete())
    body["github"].update(run_id=run_id, run_attempt=run_attempt, sha=sha)
    body.update(overrides)
    return body


def with_stage(body: dict[str, Any], name: str, status: str, exit_code: int | None = 1) -> dict[str, Any]:
    for stage in body["stages"]:
        if stage["name"] == name:
            stage.update(status=status, exit_code=exit_code)
    return body


def token(key, *, run_id: int = RUN_ID, run_attempt: int = 1, sha: str = SHA, **claims: Any) -> str:
    return oidc.sign(oidc.claims(run_id=run_id, run_attempt=run_attempt, sha=sha, **claims), key)


async def post(client: AsyncClient, body: dict[str, Any], bearer: str | None) -> Response:
    headers = {"authorization": f"Bearer {bearer}"} if bearer is not None else {}
    return await client.post("/api/v1/runs/report", json=body, headers=headers)


async def make_run_build(
    session: AsyncSession,
    project: Project,
    *,
    run_id: int = RUN_ID,
    attempt: int = 1,
    status: BuildStatus = BuildStatus.running,
    trigger: BuildTrigger = BuildTrigger.push,
    **fields: Any,
) -> Build:
    """A build bound to a workflow run, as the webhooks would leave it."""
    fields.setdefault("commit_sha", SHA)
    build = await make_build(
        session, project, trigger=trigger, gh_run_id=run_id, gh_run_attempt=attempt, **fields
    )
    if status is not BuildStatus.queued:
        await transition(session, build.id, BuildStatus.running, _at("09:01:00"))
    if status not in (BuildStatus.queued, BuildStatus.running):
        await transition(session, build.id, status, _at("09:05:00"))
    await session.commit()
    return build


def _at(hhmmss: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-14T{hhmmss}+00:00")


def artifact_zip(overrides: dict[str, bytes | None] | None = None) -> bytes:
    """The zipped out/ directory, as upload-artifact stores it. An override
    replaces a file's content, or removes it when None."""
    files = {
        p.relative_to(OUT_DIR).as_posix(): p.read_bytes() for p in sorted(OUT_DIR.rglob("*")) if p.is_file()
    }
    for name, data in (overrides or {}).items():
        if data is None:
            files.pop(name, None)
        else:
            files[name] = data
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()
