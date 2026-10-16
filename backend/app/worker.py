"""Background worker. Run with `python -m app.worker`.

Polls job_queue and runs its jobs: GitHub webhooks, artifact ingestion,
run-report deadlines and release gate evaluation. Each job runs in one transaction
that holds the job's row lock (SELECT ... FOR UPDATE SKIP LOCKED), so several
workers can poll safely and a crashed worker's job simply becomes visible
again. The handler runs inside a savepoint: if it fails, its writes are
undone but the attempt count, backoff and error are still committed.

Every settings.reconcile_interval_s (and once at startup) it also runs a
reconcile pass that repairs builds whose webhooks went missing (see
services.reconciler). The worker also writes a heartbeat file for the
container healthcheck.
"""
import asyncio
import logging
import os
import signal
import socket
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import make_engine, make_sessionmaker
from app.logs import configure_logging
from app.models import Job, JobStatus, WebhookEvent
from app.services import artifacts, gates, github_events, outcome, reconciler
from app.services.github import GitHubApp
from app.services.webhooks import JOB_KIND

log = logging.getLogger("sfci.worker")

BACKOFF_BASE = timedelta(seconds=5)
BACKOFF_MAX = timedelta(minutes=15)
MAX_ERROR_LEN = 2000


def backoff(attempts: int) -> timedelta:
    """Delay before retry number `attempts` (1-based): 5s, 10s, 20s, ... capped."""
    return min(BACKOFF_BASE * 2 ** (attempts - 1), BACKOFF_MAX)


async def handle_job(
    session: AsyncSession, job: Job, settings: Settings, github: GitHubApp | None
) -> None:
    if job.kind == JOB_KIND:
        event = await session.get(WebhookEvent, job.payload["delivery_id"])
        if event is None:
            raise LookupError(f"webhook event {job.payload['delivery_id']} not found")
        await github_events.handle(session, event.event, event.payload, settings)
    elif job.kind == artifacts.INGEST_JOB_KIND:
        if github is None:
            raise RuntimeError("no GitHub client in this worker")
        await artifacts.ingest(session, github, settings, job.payload["build_id"])
        # Gates need the verified artifacts; the build may already have passed.
        await gates.queue_if_passed(session, job.payload["build_id"])
    elif job.kind == gates.EVALUATE_JOB_KIND:
        await gates.evaluate_build(session, github, settings, job.payload["build_id"])
    elif job.kind == outcome.DEADLINE_JOB_KIND:
        await outcome.handle_deadline(session, job.payload["build_id"], settings)
    else:
        raise ValueError(f"unknown job kind: {job.kind}")


async def process_one(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    worker_id: str,
    github: GitHubApp | None = None,
) -> bool:
    """Process the next ready job, if any. Returns whether a job was found."""
    async with sessionmaker() as session, session.begin():
        job = await session.scalar(
            select(Job)
            .where(Job.status == JobStatus.queued, Job.run_after <= func.now())
            .order_by(Job.run_after, Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return False
        job_id, job_kind, attempts, max_attempts = job.id, job.kind, job.attempts + 1, job.max_attempts
        delivery_id = job.payload.get("delivery_id")

        error: str | None = None
        try:
            async with session.begin_nested():
                await handle_job(session, job, settings, github)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LEN]

        bookkeeping = {"attempts": attempts, "locked_at": func.now(), "locked_by": worker_id}
        if error is None:
            job_values = {**bookkeeping, "status": JobStatus.done, "last_error": None}
        elif attempts >= max_attempts:
            job_values = {**bookkeeping, "status": JobStatus.failed, "last_error": error}
        else:
            job_values = {
                **bookkeeping,
                "last_error": error,
                "run_after": func.now() + backoff(attempts),
            }
        await session.execute(update(Job).where(Job.id == job_id).values(**job_values))

        if delivery_id is not None:
            event_values = {"attempts": WebhookEvent.attempts + 1, "last_error": error}
            if error is None:
                event_values["processed_at"] = func.now()
            await session.execute(
                update(WebhookEvent)
                .where(WebhookEvent.delivery_id == delivery_id)
                .values(**event_values)
            )

    extra = {"job_id": job_id, "kind": job_kind, "delivery_id": delivery_id, "attempt": attempts}
    if error is None:
        log.info("job done", extra=extra)
    elif attempts >= max_attempts:
        log.error("job failed permanently", extra={**extra, "error": error})
    else:
        log.warning("job failed, will retry", extra={**extra, "error": error})
    return True


async def run(settings: Settings, stop: asyncio.Event) -> None:
    engine = make_engine(settings)
    sessionmaker = make_sessionmaker(engine)
    github = GitHubApp(settings)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    heartbeat = Path(settings.worker_heartbeat_path)
    log.info(
        "worker started",
        extra={
            "poll_interval_s": settings.worker_poll_interval_s,
            "reconcile_interval_s": settings.reconcile_interval_s if github.configured else None,
        },
    )
    if not github.configured:
        log.warning("reconciler disabled: GitHub App is not configured")
    next_reconcile = time.monotonic()
    try:
        while not stop.is_set():
            try:
                # Drain every ready job before sleeping.
                while not stop.is_set() and await process_one(
                    sessionmaker, settings, worker_id, github
                ):
                    heartbeat.touch()
                heartbeat.touch()
            except Exception:
                log.exception("worker poll failed")
            if github.configured and not stop.is_set() and time.monotonic() >= next_reconcile:
                next_reconcile = time.monotonic() + settings.reconcile_interval_s
                try:
                    await reconciler.reconcile(sessionmaker, settings, github, heartbeat.touch)
                except Exception:
                    log.exception("reconcile pass failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_interval_s)
            except TimeoutError:
                pass
    finally:
        await github.aclose()
        await engine.dispose()
        log.info("worker stopped")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run(settings, stop)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
