import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import WebhookAck
from app.services.webhooks import record_delivery, verify_signature

router = APIRouter(tags=["webhooks"])
log = logging.getLogger(__name__)


@router.post(
    "/webhooks/github",
    status_code=202,
    response_model=WebhookAck,
    responses={200: {"model": WebhookAck, "description": "Duplicate delivery, ignored"}},
)
async def github_webhook(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
) -> WebhookAck:
    """Receive a GitHub webhook. The signature is checked over the raw body
    before anything is parsed; work happens later in the worker."""
    secret = request.app.state.settings.github_webhook_secret
    if secret is None:
        log.error("webhook rejected: SFCI_GITHUB_WEBHOOK_SECRET is not set")
        raise HTTPException(status_code=503, detail="webhook secret not configured")

    body = await request.body()
    if not verify_signature(secret.get_secret_value().encode(), body, x_hub_signature_256):
        log.warning("webhook rejected: bad signature", extra={"delivery_id": x_github_delivery})
        raise HTTPException(status_code=401, detail="invalid signature")

    if not x_github_event or not x_github_delivery:
        raise HTTPException(status_code=400, detail="missing X-GitHub-Event or X-GitHub-Delivery")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="body is not JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body is not a JSON object")

    async with session.begin():
        is_new = await record_delivery(
            session, delivery_id=x_github_delivery, event=x_github_event, payload=payload
        )

    extra = {"delivery_id": x_github_delivery, "event": x_github_event}
    if not is_new:
        log.info("webhook duplicate", extra=extra)
        response.status_code = 200
        return WebhookAck(status="duplicate")
    log.info("webhook accepted", extra=extra)
    return WebhookAck(status="accepted")
