"""FastAPI application factory. Run with:

    uvicorn app.main:create_app --factory
"""
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.api.v1 import router as api_v1_router
from app.config import Settings, get_settings
from app.db import make_engine, make_sessionmaker
from app.logs import configure_logging
from app.services.github import GitHubApp
from app.services.oidc import GitHubOidcVerifier

log = logging.getLogger("sfci.http")


class RequestLogMiddleware(BaseHTTPMiddleware):
    """One structured log line per request. Logs the path only: query strings
    and headers can carry tokens and are never logged."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["x-request-id"] = request_id
            return response
        finally:
            log.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                },
            )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    engine = make_engine(settings)
    github = GitHubApp(settings)
    oidc = GitHubOidcVerifier(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.getLogger("sfci").info("api started", extra={"env": settings.env})
        yield
        await github.aclose()
        await oidc.aclose()
        await engine.dispose()

    app = FastAPI(title="SiliconFlow CI", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.github = github
    app.state.oidc = oidc
    app.add_middleware(RequestLogMiddleware)
    app.include_router(api_v1_router)
    return app
