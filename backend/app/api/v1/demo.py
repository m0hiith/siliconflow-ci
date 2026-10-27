"""Demo-only endpoints (docs/demo.md). Outside demo mode every route here
answers 404, exactly like a path that does not exist."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.demo.replay import DemoReplay, ScenarioRun
from app.demo.scenarios import Scenario
from app.schemas import DemoEventOut, DemoScenarioOut

router = APIRouter(prefix="/demo", tags=["demo"])


def demo_replay(request: Request) -> DemoReplay:
    replay = getattr(request.app.state, "demo_replay", None)
    if not request.app.state.settings.demo_mode or replay is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return replay


Replay = Annotated[DemoReplay, Depends(demo_replay)]


async def _out(replay: DemoReplay, scenario: Scenario) -> DemoScenarioOut:
    run: ScenarioRun | None = replay.runs.get(scenario.name)
    build_ids = await replay.build_ids(scenario)
    if run is not None and run.state != "done":
        state = run.state
    elif len(build_ids) == len(scenario.builds):
        state = "done"
    else:
        state = "idle"
    return DemoScenarioOut(
        name=scenario.name,
        title=scenario.title,
        summary=scenario.summary,
        state=state,
        build_ids={**build_ids, **(run.build_ids if run else {})},
        events=[DemoEventOut(**e) for e in run.events] if run else [],
        error=run.error if run else None,
    )


@router.get("/scenarios", response_model=list[DemoScenarioOut])
async def list_scenarios(replay: Replay) -> list[DemoScenarioOut]:
    return [await _out(replay, s) for s in replay.fixtures.scenarios.values()]


@router.post(
    "/scenarios/{name}",
    status_code=202,
    response_model=DemoScenarioOut,
    responses={404: {"description": "Not in demo mode, or no such scenario"},
               409: {"description": "The scenario is already running"}},
)
async def start_scenario(
    name: str,
    replay: Replay,
    speed: Annotated[float, Query(ge=0.1, le=100, description="Pacing; 2 = twice as fast")] = 1.0,
) -> DemoScenarioOut:
    """Replay a scenario's recorded inputs through this API. Replaying one
    again changes nothing: its builds already exist."""
    scenario = replay.fixtures.scenarios.get(name)
    if scenario is None:
        raise HTTPException(status_code=404, detail="no such scenario")
    try:
        replay.start(name, speed)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return await _out(replay, scenario)
