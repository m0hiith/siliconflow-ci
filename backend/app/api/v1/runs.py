import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import RunReportAck, RunReportIn
from app.services.linked_reports import accept_linked_report
from app.services.oidc import GitHubOidcVerifier, InvalidToken, OidcUnavailable, RunClaims
from app.services.run_reports import ReportRejected, accept_report

router = APIRouter(tags=["runs"])
log = logging.getLogger(__name__)


async def run_claims(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> RunClaims:
    """The verified OIDC claims of the calling workflow run. As a dependency it
    runs before the body is validated, so unauthenticated callers learn nothing
    about the report schema."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401, detail="bearer token required", headers={"WWW-Authenticate": "Bearer"}
        )
    verifier: GitHubOidcVerifier = request.app.state.oidc
    try:
        return await verifier.verify(token.strip())
    except InvalidToken as exc:
        log.warning("run report rejected: invalid token", extra={"error": str(exc)})
        raise HTTPException(
            status_code=401, detail="invalid token", headers={"WWW-Authenticate": "Bearer"}
        ) from None
    except OidcUnavailable as exc:
        log.error("run report rejected: oidc keys unavailable", extra={"error": str(exc)})
        raise HTTPException(status_code=503, detail="cannot verify tokens right now") from None


@router.post(
    "/runs/report",
    status_code=202,
    response_model=RunReportAck,
    responses={
        200: {"model": RunReportAck, "description": "The same report was already recorded"},
        401: {"description": "Missing or invalid OIDC token"},
        403: {"description": "The token does not match the report or the build"},
        404: {"description": "No build for this run yet; retry"},
        409: {"description": "The build is finished (or, for a linked report, did not pass), "
                "or a different report was recorded"},
        503: {"description": "GitHub's OIDC keys could not be fetched; retry"},
    },
)
async def post_run_report(
    report: RunReportIn,
    claims: Annotated[RunClaims, Depends(run_claims)],
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RunReportAck:
    """Record a CI job's run report, authenticated by a GitHub Actions OIDC
    token (audience "siliconflow") in the Authorization header.

    A report with `profile` and `build_id` is a linked report: it comes from a
    hardware-in-the-loop workflow and adds a stage to the build it names
    (docs/hil.md)."""
    extra = {"run_id": claims.run_id, "attempt": claims.run_attempt, "repository": claims.repository}
    accept = accept_linked_report if report.build_id is not None else accept_report
    try:
        async with session.begin():
            accepted = await accept(session, claims, report, request.app.state.settings)
    except ReportRejected as exc:
        log.warning("run report rejected", extra={**extra, "status": exc.status_code, "reason": exc.detail})
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    if accepted.duplicate:
        response.status_code = 200
        return RunReportAck(status="duplicate", build_id=accepted.build_id)
    return RunReportAck(status="accepted", build_id=accepted.build_id)
