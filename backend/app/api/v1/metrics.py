from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import TERMINAL_BUILD_STATUSES, Build, BuildStatus
from app.schemas import BuildOut, MetricsSummaryOut

router = APIRouter(tags=["metrics"])

Session = Annotated[AsyncSession, Depends(get_session)]

RECENT_FAILURES = 10


def utcnow() -> datetime:
    """The current time; tests replace this to pin "today"."""
    return datetime.now(timezone.utc)


@router.get("/metrics/summary", response_model=MetricsSummaryOut)
async def metrics_summary(session: Session, project_id: str | None = None) -> MetricsSummaryOut:
    """Today's build health, computed from recorded builds. "Today" is the
    current UTC day, and a build counts on the day it was created.

    pass_rate and median_duration_seconds consider only builds with a
    firmware verdict (passed or failed); cancelled, timed_out and errored
    builds are counted in status_counts but do not move either. Both are
    null when there is no such build today."""
    now = utcnow()
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def scoped(stmt):
        return stmt.where(Build.project_id == project_id) if project_id is not None else stmt

    rows = await session.execute(
        scoped(
            select(Build.status, func.count())
            .where(Build.created_at >= window_start)
            .group_by(Build.status)
        )
    )
    status_counts = {s.value: 0 for s in BuildStatus}
    for status, n in rows:
        status_counts[status.value] = n
    passed = status_counts[BuildStatus.passed.value]
    failed = status_counts[BuildStatus.failed.value]

    duration = extract("epoch", Build.finished_at - Build.started_at)
    median = await session.scalar(
        scoped(
            select(func.percentile_cont(0.5).within_group(duration)).where(
                Build.created_at >= window_start,
                Build.status.in_([BuildStatus.passed, BuildStatus.failed]),
                Build.started_at.is_not(None),
                Build.finished_at.is_not(None),
            )
        )
    )

    # Not limited to today: a build queued yesterday may still be running.
    active = await session.scalar(
        scoped(
            select(func.count()).select_from(Build).where(
                Build.status.not_in(TERMINAL_BUILD_STATUSES)
            )
        )
    )

    recent = await session.scalars(
        scoped(
            select(Build)
            .where(Build.status == BuildStatus.failed)
            .order_by(Build.finished_at.desc().nulls_last(), Build.id.desc())
            .limit(RECENT_FAILURES)
        )
    )

    return MetricsSummaryOut(
        generated_at=now,
        window_start=window_start,
        builds_today=sum(status_counts.values()),
        status_counts=status_counts,
        failed_today=failed,
        pass_rate=passed / (passed + failed) if passed + failed else None,
        median_duration_seconds=float(median) if median is not None else None,
        active_builds=active or 0,
        recent_failures=[BuildOut.model_validate(b) for b in recent],
    )
