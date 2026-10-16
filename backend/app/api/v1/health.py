import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import HealthOut

router = APIRouter(tags=["health"])
log = logging.getLogger(__name__)


@router.get("/health", response_model=HealthOut)
async def liveness() -> HealthOut:
    return HealthOut(status="ok")


@router.get("/health/ready", response_model=HealthOut, responses={503: {"model": HealthOut}})
async def readiness(session: Annotated[AsyncSession, Depends(get_session)]):
    """Ready when the database answers and has been migrated."""
    try:
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception:
        log.warning("readiness check failed", exc_info=True)
        return JSONResponse(status_code=503, content={"status": "unavailable", "detail": {}})
    return HealthOut(status="ok", detail={"schema_version": version})
