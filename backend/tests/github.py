"""GitHub webhook test helpers.

Payloads start from real recorded examples (tests/fixtures/github, from
octokit/webhooks, the source of the examples in GitHub's webhook docs) and
override only the fields a test is about.
"""
import copy
import json
import uuid
from functools import cache
from pathlib import Path
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import update

from app.models import Job, JobStatus, Repository
from app.services.webhooks import signature_for
from app.worker import process_one
from tests.conftest import WEBHOOK_SECRET

FIXTURES = Path(__file__).parent / "fixtures" / "github"
WORKFLOW_PATH = ".github/workflows/firmware-ci.yml"
SHA = "3484a3fb816e0859fd6e1cea078d76385ff50625"


@cache
def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def fixture(name: str) -> dict[str, Any]:
    return copy.deepcopy(_fixture(name))


def _with_repo(payload: dict[str, Any], repo: Repository, gh_repo_id: int) -> dict[str, Any]:
    payload["repository"]["id"] = gh_repo_id
    payload["repository"]["full_name"] = repo.full_name
    return payload


def workflow_run(
    action: str,
    repo: Repository,
    *,
    run_id: int,
    attempt: int = 1,
    gh_repo_id: int = 300029405,
    event: str = "push",
    conclusion: str | None = None,
    display_title: str = "Firmware CI " + SHA,
    path: str = WORKFLOW_PATH,
    started_at: str = "2026-10-14T09:00:00Z",
    updated_at: str = "2026-10-14T09:05:00Z",
) -> dict[str, Any]:
    name = {
        "requested": "workflow_run.requested.json",
        "in_progress": "workflow_run.requested.json",
        "completed": "workflow_run.completed.json",
    }[action]
    payload = _with_repo(fixture(name), repo, gh_repo_id)
    payload["action"] = action
    run = payload["workflow_run"]
    run.update(
        id=run_id,
        run_attempt=attempt,
        event=event,
        display_title=display_title,
        path=path,
        run_started_at=started_at,
        updated_at=updated_at,
        status={"requested": "queued", "in_progress": "in_progress", "completed": "completed"}[action],
        conclusion=conclusion if action == "completed" else None,
    )
    return payload


def workflow_job(
    repo: Repository,
    *,
    run_id: int,
    attempt: int = 1,
    gh_repo_id: int = 300029405,
    started_at: str = "2026-10-14T09:01:00Z",
) -> dict[str, Any]:
    payload = _with_repo(fixture("workflow_job.in_progress.json"), repo, gh_repo_id)
    payload["workflow_job"].update(run_id=run_id, run_attempt=attempt, started_at=started_at)
    return payload


async def deliver(
    client: AsyncClient,
    event: str,
    payload: dict[str, Any],
    *,
    delivery_id: str | None = None,
    secret: str = WEBHOOK_SECRET,
) -> Response:
    body = json.dumps(payload).encode()
    return await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "content-type": "application/json",
            "x-github-event": event,
            "x-github-delivery": delivery_id or str(uuid.uuid4()),
            "x-hub-signature-256": signature_for(secret.encode(), body),
        },
    )


async def drain(sessionmaker, settings, github=None) -> int:
    """Run the worker until no job is ready. Returns how many jobs it ran."""
    n = 0
    while await process_one(sessionmaker, settings, "test-worker", github):
        n += 1
    return n


async def make_retries_ready(sessionmaker) -> None:
    """Pretend the backoff delay has passed."""
    async with sessionmaker() as s:
        await s.execute(
            update(Job).where(Job.status == JobStatus.queued).values(run_after=Job.created_at)
        )
        await s.commit()
