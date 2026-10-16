from fastapi import APIRouter

from app.api.v1 import analytics, artifacts, builds, health, metrics, projects, runs, webhooks

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(projects.router)
router.include_router(builds.router)
router.include_router(artifacts.router)
router.include_router(metrics.router)
router.include_router(analytics.router)
router.include_router(runs.router)
router.include_router(webhooks.router)
