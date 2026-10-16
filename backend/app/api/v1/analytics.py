"""Analytics over recorded builds. Every number is computed from stored rows
when requested: nothing is cached, interpolated or filled in, and a day,
week or commit without data has no point at all."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Date, cast, extract, func, literal_column, or_, select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import (
    TERMINAL_BUILD_STATUSES,
    Build,
    BuildSize,
    BuildStatus,
    Repository,
    TestCase,
    TestOutcome,
)
from app.schemas import (
    DurationDayOut,
    DurationsOut,
    FailureClassCountOut,
    FlakyExampleOut,
    FlakyTestOut,
    FlakyTestsOut,
    PassRateOut,
    PassRateWeekOut,
    SizeCommitOut,
    SizeSeriesOut,
    SizeTrendsOut,
)
from app.services.analytics import MIN_BUILDS, day_start, failed, flip_indices, week_start

router = APIRouter(prefix="/analytics", tags=["analytics"])

Session = Annotated[AsyncSession, Depends(get_session)]

SIZE_PROFILES = ["cortex-m4", "siwx917-soc"]
FLAKY_RECENT_BUILDS = 20
FLAKY_FLIP_THRESHOLD = 3  # more than this many flips is flaky
FLAKY_SAME_COMMIT_EXAMPLES = 3  # commits shown per test
FLAKY_FLIP_EXAMPLES = 4

VERDICT_STATUSES = (BuildStatus.passed, BuildStatus.failed)
NOT_PASSED_STATUSES = (BuildStatus.failed, BuildStatus.timed_out, BuildStatus.errored)

# date_trunc's unit and the zone are literals so GROUP BY sees the same
# expression as SELECT (bound parameters would differ).
_utc_created = func.timezone(literal_column("'UTC'"), Build.created_at)
CREATED_DAY = cast(func.date_trunc(literal_column("'day'"), _utc_created), Date)
CREATED_WEEK = cast(func.date_trunc(literal_column("'week'"), _utc_created), Date)

# The build ran on its repository's default branch (a push, not a PR).
ON_DEFAULT_BRANCH = Build.ref == func.concat("refs/heads/", Repository.default_branch)


def utcnow() -> datetime:
    """The current time; tests replace this to pin "today"."""
    return datetime.now(timezone.utc)


def scoped(stmt, project_id: str | None):
    return stmt.where(Build.project_id == project_id) if project_id is not None else stmt


ProjectId = Annotated[str | None, Query(description="Limit to one project")]


@router.get("/durations", response_model=DurationsOut)
async def durations(
    session: Session,
    project_id: ProjectId = None,
    days: Annotated[int, Query(ge=1, le=365, description="UTC days, today included")] = 30,
) -> DurationsOut:
    """Median and p90 started-to-finished time per profile and UTC day of
    creation, over passed and failed builds (cancelled, timed_out and errored
    builds say nothing about how long the firmware takes to build and test)."""
    now = utcnow()
    start = day_start(now) - timedelta(days=days - 1)
    duration = extract("epoch", Build.finished_at - Build.started_at)
    rows = (
        await session.execute(
            scoped(
                select(
                    Build.profile,
                    CREATED_DAY,
                    func.count(),
                    func.percentile_cont(0.5).within_group(duration),
                    func.percentile_cont(0.9).within_group(duration),
                )
                .where(
                    Build.created_at >= start,
                    Build.status.in_(VERDICT_STATUSES),
                    Build.started_at.is_not(None),
                    Build.finished_at.is_not(None),
                )
                .group_by(Build.profile, CREATED_DAY)
                .order_by(CREATED_DAY, Build.profile),
                project_id,
            )
        )
    ).all()
    builds = sum(n for _, _, n, _, _ in rows)
    enough = builds >= MIN_BUILDS
    return DurationsOut(
        generated_at=now,
        window_start=start,
        builds=builds,
        min_builds=MIN_BUILDS,
        enough_data=enough,
        days=[
            DurationDayOut(
                profile=profile, day=day, builds=n, median_seconds=p50, p90_seconds=p90
            )
            for profile, day, n, p50, p90 in rows
        ]
        if enough
        else [],
    )


@router.get("/pass-rate", response_model=PassRateOut)
async def pass_rate(
    session: Session,
    project_id: ProjectId = None,
    weeks: Annotated[int, Query(ge=1, le=104, description="ISO weeks, this one included")] = 12,
) -> PassRateOut:
    """Per ISO week (UTC, by creation): terminal builds by status, the pass
    rate over builds with a firmware verdict, and the failure classes of the
    builds that did not pass."""
    now = utcnow()
    start = week_start(now) - timedelta(weeks=weeks - 1)
    in_window = (Build.created_at >= start, Build.status.in_(TERMINAL_BUILD_STATUSES))

    counts: dict = defaultdict(
        lambda: {s.value: 0 for s in BuildStatus if s in TERMINAL_BUILD_STATUSES}
    )
    for week, status, n in await session.execute(
        scoped(
            select(CREATED_WEEK, Build.status, func.count())
            .where(*in_window)
            .group_by(CREATED_WEEK, Build.status),
            project_id,
        )
    ):
        counts[week][status.value] = n

    classes: dict = defaultdict(list)
    for week, failure_class, n in await session.execute(
        scoped(
            select(CREATED_WEEK, Build.failure_class, func.count())
            .where(*in_window, Build.status.in_(NOT_PASSED_STATUSES))
            .group_by(CREATED_WEEK, Build.failure_class)
            .order_by(CREATED_WEEK, func.count().desc(), Build.failure_class.nulls_last()),
            project_id,
        )
    ):
        classes[week].append(FailureClassCountOut(failure_class=failure_class, count=n))

    builds = sum(sum(c.values()) for c in counts.values())
    enough = builds >= MIN_BUILDS
    weeks_out = []
    for week in sorted(counts) if enough else []:
        c = counts[week]
        passed, failed_n = c[BuildStatus.passed.value], c[BuildStatus.failed.value]
        weeks_out.append(
            PassRateWeekOut(
                week_start=week,
                builds=sum(c.values()),
                status_counts=c,
                pass_rate=passed / (passed + failed_n) if passed + failed_n else None,
                failure_classes=classes[week],
            )
        )
    return PassRateOut(
        generated_at=now,
        window_start=start,
        builds=builds,
        min_builds=MIN_BUILDS,
        enough_data=enough,
        weeks=weeks_out,
    )


@router.get("/sizes", response_model=SizeTrendsOut)
async def sizes(
    session: Session,
    project_id: ProjectId = None,
    profile: Annotated[
        list[str] | None, Query(description="Repeat for several; default cortex-m4 and siwx917-soc")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500, description="Latest commits per profile")] = 100,
) -> SizeTrendsOut:
    """Flash (text + data) and RAM (data + bss) per commit on the default
    branch, from the sizes run reports stated. A commit built more than once
    is shown by its latest build that reported a size, whatever its status."""
    profiles = list(dict.fromkeys(profile or SIZE_PROFILES))
    series = []
    for p in profiles:
        sized = scoped(
            select(Build.id)
            .join(BuildSize, BuildSize.build_id == Build.id)
            .join(Repository, Repository.id == Build.repository_id)
            .where(Build.profile == p, ON_DEFAULT_BRANCH, Build.commit_sha.is_not(None)),
            project_id,
        )
        builds = await session.scalar(select(func.count()).select_from(sized.subquery())) or 0
        enough = builds >= MIN_BUILDS
        commits: list[SizeCommitOut] = []
        if enough:
            latest = (
                sized.ext(distinct_on(Build.repository_id, Build.commit_sha))
                .order_by(
                    Build.repository_id, Build.commit_sha, Build.created_at.desc(), Build.id.desc()
                )
                .subquery()
            )
            rows = await session.execute(
                select(Build, BuildSize)
                .join(BuildSize, BuildSize.build_id == Build.id)
                .where(Build.id.in_(select(latest.c.id)))
                .order_by(Build.created_at.desc(), Build.id.desc())
                .limit(limit)
            )
            commits = [
                SizeCommitOut(
                    build_id=b.id,
                    build_number=b.number,
                    status=b.status,
                    commit_sha=b.commit_sha,
                    created_at=b.created_at,
                    text_bytes=s.text_bytes,
                    data_bytes=s.data_bytes,
                    bss_bytes=s.bss_bytes,
                    flash_bytes=s.text_bytes + s.data_bytes,
                    ram_bytes=s.data_bytes + s.bss_bytes,
                )
                for b, s in reversed(rows.all())
            ]
        series.append(SizeSeriesOut(profile=p, builds=builds, enough_data=enough, commits=commits))
    return SizeTrendsOut(generated_at=utcnow(), min_builds=MIN_BUILDS, series=series)


@router.get("/flaky-tests", response_model=FlakyTestsOut)
async def flaky_tests(
    session: Session,
    project_id: ProjectId = None,
    days: Annotated[
        int, Query(ge=1, le=365, description="Window for same-commit pass and failure")
    ] = 30,
) -> FlakyTestsOut:
    """Tests (per repository, profile, suite and name) that are flaky by
    either rule: they passed and failed on the same commit since
    window_start, or their verdict flipped more than 3 times over the last 20
    terminal default-branch builds of their profile (whenever those ran)."""
    now = utcnow()
    start = day_start(now) - timedelta(days=days - 1)
    ran = TestCase.outcome != TestOutcome.skipped

    rank = (
        func.row_number()
        .over(
            partition_by=(Build.repository_id, Build.profile),
            order_by=(Build.created_at.desc(), Build.id.desc()),
        )
        .label("rank")
    )
    ranked = scoped(
        select(Build.id, rank)
        .join(Repository, Repository.id == Build.repository_id)
        .where(ON_DEFAULT_BRANCH, Build.status.in_(TERMINAL_BUILD_STATUSES)),
        project_id,
    ).subquery()
    recent_ids = select(ranked.c.id).where(ranked.c.rank <= FLAKY_RECENT_BUILDS)

    builds = (
        await session.scalar(
            scoped(
                select(func.count(func.distinct(TestCase.build_id)))
                .join(Build, Build.id == TestCase.build_id)
                .where(or_(Build.created_at >= start, Build.id.in_(recent_ids))),
                project_id,
            )
        )
        or 0
    )
    enough = builds >= MIN_BUILDS
    result = FlakyTestsOut(
        generated_at=now,
        window_start=start,
        builds=builds,
        min_builds=MIN_BUILDS,
        enough_data=enough,
        recent_builds=FLAKY_RECENT_BUILDS,
        flip_threshold=FLAKY_FLIP_THRESHOLD,
        tests=[],
    )
    if not enough:
        return result

    Key = tuple[str, str, str, str]  # repository_id, profile, suite, name
    same_commit: dict[Key, list[FlakyExampleOut]] = defaultdict(list)
    shas: dict[Key, list[str]] = defaultdict(list)
    flips: dict[Key, int] = {}
    flip_examples: dict[Key, list[FlakyExampleOut]] = {}

    def example(build_id, number, sha, outcome, created_at) -> FlakyExampleOut:
        return FlakyExampleOut(
            build_id=build_id,
            build_number=number,
            commit_sha=sha,
            outcome=outcome,
            created_at=created_at,
        )

    # Rule 1: passed and failed on one commit.
    key_cols = (Build.repository_id, Build.profile, TestCase.suite, TestCase.name)
    pairs = (
        await session.execute(
            scoped(
                select(*key_cols, Build.commit_sha)
                .join(Build, Build.id == TestCase.build_id)
                .where(Build.created_at >= start, Build.commit_sha.is_not(None), ran)
                .group_by(*key_cols, Build.commit_sha)
                .having(
                    func.bool_or(TestCase.outcome == TestOutcome.passed),
                    func.bool_or(TestCase.outcome.in_((TestOutcome.failed, TestOutcome.error))),
                ),
                project_id,
            )
        )
    ).all()
    if pairs:
        rows = await session.execute(
            scoped(
                select(
                    *key_cols, Build.commit_sha, Build.id, Build.number, TestCase.outcome,
                    Build.created_at,
                )
                .join(Build, Build.id == TestCase.build_id)
                .where(
                    Build.created_at >= start,
                    ran,
                    tuple_(*key_cols, Build.commit_sha).in_([tuple(r) for r in pairs]),
                )
                .order_by(Build.created_at.desc(), Build.id.desc()),
                project_id,
            )
        )
        # Newest first: per commit, keep its latest pass and latest failure.
        seen: set[tuple[Key, str, bool]] = set()
        for repo_id, prof, suite, name, sha, build_id, number, outcome, created_at in rows:
            key = (repo_id, prof, suite, name)
            if sha not in shas[key]:
                shas[key].append(sha)
            if (key, sha, failed(outcome)) in seen:
                continue
            seen.add((key, sha, failed(outcome)))
            if shas[key].index(sha) < FLAKY_SAME_COMMIT_EXAMPLES:
                same_commit[key].append(example(build_id, number, sha, outcome, created_at))

    # Rule 2: flipped over the recent default-branch builds.
    history: dict[Key, list] = defaultdict(list)
    for repo_id, prof, suite, name, build_id, number, sha, outcome, created_at in (
        await session.execute(
            select(*key_cols, Build.id, Build.number, Build.commit_sha, TestCase.outcome,
                   Build.created_at)
            .join(Build, Build.id == TestCase.build_id)
            .where(Build.id.in_(recent_ids), ran)
            .order_by(Build.created_at, Build.id)
        )
    ):
        history[(repo_id, prof, suite, name)].append((build_id, number, sha, outcome, created_at))
    for key, runs in history.items():
        at = flip_indices([r[3] for r in runs])
        if len(at) > FLAKY_FLIP_THRESHOLD:
            flips[key] = len(at)
            flip_examples[key] = [example(*runs[i]) for i in reversed(at)][:FLAKY_FLIP_EXAMPLES]

    keys = set(shas) | set(flips)
    repos = dict(
        (
            await session.execute(
                select(Repository.id, Repository.full_name).where(
                    Repository.id.in_({k[0] for k in keys})
                )
            )
        ).all()
    ) if keys else {}
    for key in keys:
        examples, ids = [], set()
        for e in same_commit[key] + flip_examples.get(key, []):
            if e.build_id not in ids:
                ids.add(e.build_id)
                examples.append(e)
        result.tests.append(
            FlakyTestOut(
                repository=repos[key[0]],
                profile=key[1],
                suite=key[2],
                name=key[3],
                same_commit_shas=shas.get(key, []),
                flips=flips.get(key),
                examples=examples,
            )
        )
    # Most evidence first.
    result.tests.sort(
        key=lambda t: (-(len(t.same_commit_shas) + (t.flips or 0)), t.profile, t.suite, t.name)
    )
    return result
