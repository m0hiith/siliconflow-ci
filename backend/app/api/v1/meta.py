from fastapi import APIRouter, Request

from app.schemas import MetaOut

router = APIRouter(tags=["meta"])


@router.get("/meta", response_model=MetaOut)
async def meta(request: Request) -> MetaOut:
    """What kind of instance this is. The UI shows the demo banner from this."""
    return MetaOut(demo_mode=request.app.state.settings.demo_mode, version=request.app.version)
