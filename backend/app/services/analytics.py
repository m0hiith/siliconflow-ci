"""Pure helpers for the analytics endpoints (api/v1/analytics.py). No I/O."""
from collections.abc import Sequence
from datetime import datetime, timedelta

from app.models import TestOutcome

# Below this many builds an analysis says "not enough data" instead of a result.
MIN_BUILDS = 10


def day_start(t: datetime) -> datetime:
    """Midnight starting t's day (t is UTC)."""
    return t.replace(hour=0, minute=0, second=0, microsecond=0)


def week_start(t: datetime) -> datetime:
    """Midnight starting the Monday of t's ISO week (t is UTC); Postgres's
    date_trunc('week', ...) agrees."""
    return day_start(t) - timedelta(days=t.weekday())


def failed(outcome: TestOutcome) -> bool:
    """A test that errored did not pass either; flakiness treats it as failed."""
    return outcome in (TestOutcome.failed, TestOutcome.error)


def flip_indices(outcomes: Sequence[TestOutcome]) -> list[int]:
    """Positions where a test's verdict differs from its previous run's, over
    runs in chronological order. Skipped runs carry no verdict: they are
    neither counted nor break a sequence (pass, skip, fail is one flip)."""
    flips: list[int] = []
    previous: bool | None = None
    for i, outcome in enumerate(outcomes):
        if outcome == TestOutcome.skipped:
            continue
        verdict = failed(outcome)
        if previous is not None and verdict != previous:
            flips.append(i)
        previous = verdict
    return flips
