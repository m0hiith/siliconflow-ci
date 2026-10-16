"""The only place a build's status changes.

Every move is one guarded UPDATE: the WHERE clause lists the statuses the
build may move *from*, so a stale or out-of-order event simply matches no row.
There is no read-modify-write window, and concurrent callers cannot race a
build backwards or out of a terminal state.
"""
from datetime import datetime

from sqlalchemy import any_, bindparam, func, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from app.models import TERMINAL_BUILD_STATUSES, Build, BuildStatus

S = BuildStatus

ALLOWED_MOVES: dict[BuildStatus, frozenset[BuildStatus]] = {
    S.queued: frozenset({S.running, S.cancelled, S.errored}),
    S.running: frozenset({S.passed, S.failed, S.cancelled, S.timed_out, S.errored}),
    **{terminal: frozenset() for terminal in TERMINAL_BUILD_STATUSES},
}

# The same table inverted: target status -> statuses it may be reached from.
ALLOWED_FROM: dict[BuildStatus, frozenset[BuildStatus]] = {
    to: frozenset(src for src, targets in ALLOWED_MOVES.items() if to in targets)
    for to in BuildStatus
}

# Columns a transition can change, read back to refresh loaded objects.
RETURNED = (
    Build.status,
    Build.started_at,
    Build.finished_at,
    Build.status_reason,
    Build.updated_at,
)


def can_transition(from_status: BuildStatus, to_status: BuildStatus) -> bool:
    return to_status in ALLOWED_MOVES[from_status]


async def transition(
    session: AsyncSession,
    build_id: str,
    to_status: BuildStatus,
    at: datetime,
    *,
    reason: str | None = None,
) -> bool:
    """Move a build to `to_status` if the current status allows it.

    `at` is when the move happened (from the event, not the wall clock). It
    sets started_at on entering running and finished_at on entering a
    terminal state; an existing timestamp is never overwritten. Returns
    whether the build changed. The caller commits.
    """
    allowed_from = ALLOWED_FROM[to_status]
    if not allowed_from:
        return False

    values: dict = {"status": to_status}
    if to_status is S.running:
        values["started_at"] = func.coalesce(Build.started_at, at)
    if to_status in TERMINAL_BUILD_STATUSES:
        # GREATEST ignores NULLs; it keeps finished_at >= started_at even if
        # GitHub's completion timestamp is a hair earlier than a job's start.
        values["finished_at"] = func.coalesce(
            Build.finished_at, func.greatest(at, Build.started_at)
        )
    if reason is not None:
        values["status_reason"] = reason

    stmt = (
        update(Build)
        .where(
            Build.id == build_id,
            Build.status
            == any_(bindparam("allowed_from", sorted(allowed_from), type_=ARRAY(Build.status.type))),
        )
        .values(**values)
        .returning(*RETURNED)
        .execution_options(synchronize_session=False)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return False
    # Refresh a Build already loaded in this session in place. Expiring it
    # instead would make the next attribute access lazy-load, which async
    # sessions cannot do.
    loaded = session.identity_map.get(identity_key(Build, build_id))
    if loaded is not None:
        for column, value in zip(RETURNED, row, strict=True):
            set_committed_value(loaded, column.key, value)
    return True
