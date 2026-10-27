"""Replay a demo scenario through the API's own endpoints.

For each build the replay does what GitHub and the runner would do, in order:

  1. deliver workflow_run "requested"      -> POST /api/v1/webhooks/github
  2. deliver workflow_job "in_progress"    -> POST /api/v1/webhooks/github
  3. post the job's run report             -> POST /api/v1/runs/report
  4. deliver workflow_run "completed"      -> POST /api/v1/webhooks/github
     (`deliveries` times, with one X-GitHub-Delivery, as a redelivery does)
  5. for a build with a HIL run, post its linked report.

Requests go through the ASGI app in this process, with the full middleware,
HMAC check, OIDC verification, deduplication and validation; the replay only
holds the demo signers (app.demo.trust), which never leave the process. It
never writes a build, stage, artifact or gate row itself. The one row it
writes directly is the demo project and repository, which in a real
deployment an operator registers.

The worker does the rest, as it does for GitHub's deliveries, so the UI shows
each build move queued -> running -> its terminal status.

Replaying a scenario again is harmless: a build that already exists keeps the
times it was first replayed at, so its run report is byte-identical and is
answered "duplicate", and every webhook finds the build it already made.
"""
import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.demo.scenarios import (
    DEMO_PROJECT_NAME,
    DEMO_PROJECT_SLUG,
    DEMO_REPOSITORY,
    DEMO_REPOSITORY_ID,
    HIL_WORKFLOW,
    DemoFixtures,
    Scenario,
    ScenarioBuild,
    at,
    hil_report,
    report_end,
    run_report,
    workflow_job,
    workflow_run,
)
from app.demo.trust import DemoTrust
from app.models import Build, Project, Repository
from app.services.webhooks import signature_for

log = logging.getLogger(__name__)

# Seconds between steps at speed 1, so a build is seen queued, then running.
QUEUED_S = 2.0
RUNNING_S = 4.0
SETTLE_S = 1.0
BETWEEN_BUILDS_S = 1.0
# A report can overtake the worker's handling of "requested" (404), and a HIL
# report the build's completion (409); the runner's script retries both.
RETRY_S = 0.5
RETRY_FOR_S = 120.0
# The jobs start a little after their run is requested.
JOB_START = timedelta(seconds=5)
STAGES_START = timedelta(seconds=10)
COMPLETED_AFTER = timedelta(seconds=5)

Pause = Callable[[float], Awaitable[None]]


class ReplayFailed(Exception):
    pass


@dataclass
class ScenarioRun:
    name: str
    state: Literal["running", "done", "failed"] = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    build_ids: dict[str, str] = field(default_factory=dict)
    # What each request got back, for the tour (e.g. "duplicate" on a redelivery).
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


async def ensure_demo_repository(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session, session.begin():
        project = await session.scalar(select(Project).where(Project.slug == DEMO_PROJECT_SLUG))
        if project is None:
            project = Project(slug=DEMO_PROJECT_SLUG, name=DEMO_PROJECT_NAME)
            session.add(project)
            await session.flush()
        if await session.scalar(select(Repository).where(Repository.full_name == DEMO_REPOSITORY)) is None:
            session.add(
                Repository(project_id=project.id, full_name=DEMO_REPOSITORY, gh_repo_id=DEMO_REPOSITORY_ID)
            )


class DemoReplay:
    def __init__(self, app: FastAPI, fixtures: DemoFixtures, trust: DemoTrust) -> None:
        self._app = app
        self.fixtures = fixtures
        self._trust = trust
        self.runs: dict[str, ScenarioRun] = {}
        self._tasks: set[asyncio.Task] = set()

    @property
    def _settings(self):
        return self._app.state.settings

    async def build_ids(self, scenario: Scenario) -> dict[str, str]:
        """The builds this database already has for the scenario's runs."""
        runs = {(b.run_id, b.attempt): b.key for b in scenario.builds}
        async with self._app.state.sessionmaker() as session:
            rows = await session.execute(
                select(Build.gh_run_id, Build.gh_run_attempt, Build.id)
                .join(Repository, Repository.id == Build.repository_id)
                .where(Repository.full_name == DEMO_REPOSITORY, Build.gh_run_id.in_([r for r, _ in runs]))
            )
        return {runs[(r, a)]: build_id for r, a, build_id in rows if (r, a) in runs}

    async def _first_queued_at(self, build: ScenarioBuild) -> datetime | None:
        async with self._app.state.sessionmaker() as session:
            return await session.scalar(
                select(Build.queued_at)
                .join(Repository, Repository.id == Build.repository_id)
                .where(
                    Repository.full_name == DEMO_REPOSITORY,
                    Build.gh_run_id == build.run_id,
                    Build.gh_run_attempt == build.attempt,
                )
            )

    def start(self, name: str, speed: float) -> ScenarioRun:
        """Replay a scenario in the background. Raises KeyError for an unknown
        scenario, RuntimeError if it is already running."""
        scenario = self.fixtures.scenarios[name]
        current = self.runs.get(name)
        if current is not None and current.state == "running":
            raise RuntimeError(f"scenario {name} is already running")
        run = ScenarioRun(name)
        self.runs[name] = run

        async def sleep(seconds: float) -> None:
            await asyncio.sleep(seconds / speed)

        task = asyncio.create_task(self.replay(scenario, sleep, run))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def replay(self, scenario: Scenario, pause: Pause, run: ScenarioRun | None = None) -> ScenarioRun:
        run = run or ScenarioRun(scenario.name)
        self.runs[scenario.name] = run
        log.warning("demo replay started", extra={"scenario": scenario.name})
        try:
            await ensure_demo_repository(self._app.state.sessionmaker)
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(transport=transport, base_url="http://sfci-demo-replay") as client:
                now = datetime.now(UTC)
                for i, build in enumerate(scenario.builds):
                    if i:
                        await pause(BETWEEN_BUILDS_S)
                    await self._replay_build(client, build, now, pause, run)
        except asyncio.CancelledError:
            run.state, run.error = "failed", "cancelled"
            raise
        except Exception as exc:
            run.state, run.error = "failed", f"{type(exc).__name__}: {exc}"
            log.exception("demo replay failed", extra={"scenario": scenario.name})
        else:
            run.state = "done"
            log.warning("demo replay done", extra={"scenario": scenario.name})
        finally:
            run.finished_at = datetime.now(UTC)
        return run

    async def _replay_build(
        self,
        client: httpx.AsyncClient,
        build: ScenarioBuild,
        now: datetime,
        pause: Pause,
        run: ScenarioRun,
    ) -> None:
        start = await self._first_queued_at(build) or at(build.days_ago, now)
        report = run_report(self.fixtures, build, start + STAGES_START)
        end = report_end(report, start + STAGES_START) + COMPLETED_AFTER

        requested = workflow_run(self.fixtures, build, "requested", started=start, updated=start)
        await self._deliver(client, run, build, "workflow_run", requested)
        await pause(QUEUED_S)
        job = workflow_job(self.fixtures, build, started=start + JOB_START)
        await self._deliver(client, run, build, "workflow_job", job)
        await pause(RUNNING_S)

        token = self._trust.token(self._settings.oidc_audience, self._job_claims(build))
        ack = await self._post_report(client, run, build, report, token, pause, retry_on=404)
        run.build_ids[build.key] = ack["build_id"]
        await pause(SETTLE_S)

        completed = workflow_run(self.fixtures, build, "completed", started=start, updated=end)
        delivery_id = str(uuid.uuid4())
        for n in range(build.deliveries):
            if n:
                await pause(SETTLE_S)
            await self._deliver(client, run, build, "workflow_run", completed, delivery_id=delivery_id)

        if build.hil is not None:
            await pause(RUNNING_S)
            hil_start = end + timedelta(minutes=1)
            body = hil_report(self.fixtures, build, ack["build_id"], hil_start)
            token = self._trust.token(self._settings.oidc_audience, self._hil_claims(build))
            await self._post_report(client, run, build, body, token, pause, retry_on=409)

    def _job_claims(self, build: ScenarioBuild) -> dict[str, Any]:
        workflow_ref = f"{DEMO_REPOSITORY}/{build.workflow}@{build.ref}"
        return self._claims(build.run_id, build.attempt, build.sha, build.ref, "push", workflow_ref, build.author)

    def _hil_claims(self, build: ScenarioBuild) -> dict[str, Any]:
        assert build.hil is not None
        workflow_ref = f"{DEMO_REPOSITORY}/{HIL_WORKFLOW}@refs/heads/main"
        return self._claims(
            build.hil.run_id, 1, build.sha, "refs/heads/main", "workflow_run", workflow_ref, build.author
        )

    @staticmethod
    def _claims(
        run_id: int, attempt: int, sha: str, ref: str, event: str, workflow_ref: str, actor: str
    ) -> dict[str, Any]:
        """The claims GitHub puts in a job's OIDC token (run ids are strings there)."""
        return {
            "sub": f"repo:{DEMO_REPOSITORY}:ref:{ref}",
            "repository": DEMO_REPOSITORY,
            "repository_id": str(DEMO_REPOSITORY_ID),
            "repository_owner": DEMO_REPOSITORY.split("/")[0],
            "run_id": str(run_id),
            "run_attempt": str(attempt),
            "sha": sha,
            "ref": ref,
            "ref_type": "branch",
            "event_name": event,
            "actor": actor,
            "workflow_ref": workflow_ref,
            "job_workflow_ref": workflow_ref,
            "runner_environment": "github-hosted",
        }

    async def _deliver(
        self,
        client: httpx.AsyncClient,
        run: ScenarioRun,
        build: ScenarioBuild,
        event: str,
        payload: dict[str, Any],
        *,
        delivery_id: str | None = None,
    ) -> None:
        body = json.dumps(payload).encode()
        delivery_id = delivery_id or str(uuid.uuid4())
        secret = self._settings.github_webhook_secret.get_secret_value().encode()
        response = await client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "content-type": "application/json",
                "x-github-event": event,
                "x-github-delivery": delivery_id,
                "x-hub-signature-256": signature_for(secret, body),
            },
        )
        step = f"{event}.{payload.get('action')}"
        result = _result(response)
        run.events.append(
            {"build": build.key, "step": step, "delivery_id": delivery_id, "status_code": response.status_code,
             "result": result}
        )
        if response.status_code not in (200, 202):
            raise ReplayFailed(f"{build.key} {step}: HTTP {response.status_code} {result}")

    async def _post_report(
        self,
        client: httpx.AsyncClient,
        run: ScenarioRun,
        build: ScenarioBuild,
        body: dict[str, Any],
        token: str,
        pause: Pause,
        *,
        retry_on: int,
    ) -> dict[str, Any]:
        step = "linked_report" if "build_id" in body else "run_report"
        deadline = time.monotonic() + RETRY_FOR_S
        while True:
            response = await client.post(
                "/api/v1/runs/report", json=body, headers={"authorization": f"Bearer {token}"}
            )
            if response.status_code != retry_on or time.monotonic() > deadline:
                break
            await pause(RETRY_S)
        result = _result(response)
        run.events.append(
            {"build": build.key, "step": step, "status_code": response.status_code, "result": result}
        )
        if response.status_code not in (200, 202):
            raise ReplayFailed(f"{build.key} {step}: HTTP {response.status_code} {result}")
        return response.json()


def _result(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("status") or body.get("detail") or "")[:200]
    return ""
