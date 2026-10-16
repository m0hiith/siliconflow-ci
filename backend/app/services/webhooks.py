"""GitHub webhook ingestion: authenticate, store once, enqueue.

The request path does no build work. It records the delivery and queues a
job; the worker (app.worker) does the rest, so GitHub gets its answer fast.
"""
import hashlib
import hmac
from typing import Any

from sqlalchemy import func, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, WebhookEvent

JOB_KIND = "github_webhook"

# Events the worker acts on. Others (ping, push, ...) are stored for the
# record but no job is queued for them.
HANDLED_EVENTS = frozenset({"workflow_run", "workflow_job"})


def signature_for(secret: bytes, body: bytes) -> str:
    """The X-Hub-Signature-256 value GitHub sends for `body`."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """Constant-time check of X-Hub-Signature-256 over the raw request body."""
    if not header:
        return False
    return hmac.compare_digest(signature_for(secret, body).encode(), header.encode())


async def record_delivery(
    session: AsyncSession,
    *,
    delivery_id: str,
    event: str,
    payload: dict[str, Any],
) -> bool:
    """Store a delivery and queue its job in one transaction.

    Returns False, doing nothing, if this delivery id was seen before; GitHub
    redelivers on timeouts and on manual "Redeliver", with the same id.
    """
    handled = event in HANDLED_EVENTS
    inserted = await session.scalar(
        pg_insert(WebhookEvent)
        .values(
            delivery_id=delivery_id,
            event=event,
            action=payload.get("action") if isinstance(payload.get("action"), str) else None,
            payload=payload,
            # Nothing to do for unhandled events: they are processed on arrival.
            processed_at=None if handled else func.now(),
        )
        .on_conflict_do_nothing(index_elements=[WebhookEvent.delivery_id])
        .returning(WebhookEvent.delivery_id)
    )
    if inserted is None:
        return False
    if handled:
        await session.execute(
            insert(Job).values(kind=JOB_KIND, payload={"delivery_id": delivery_id})
        )
    return True
