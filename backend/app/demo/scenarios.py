"""Demo scenarios: which recorded inputs to replay, as which runs.

A scenario (demo/scenarios/<name>.yml) lists builds. Each build is one
workflow run attempt of the demo repository and names a fixture: a run report
recorded from a CI job (demo/fixtures/reports/<fixture>.json) and, if that
report names an Actions artifact, the zipped out/ directory it describes
(demo/fixtures/artifacts/<zip>.zip). The replay (app.demo.replay) sends the
run's webhooks and its report; the worker's demo source (app.demo.source)
serves the zip and gates.yml when ingestion and gate evaluation ask for them.

The fixture's identity (repository, run, commit, timestamps) is rewritten to
the scenario's run. Nothing that decides an outcome is rewritten: stage
results, exit codes, JUnit files, logs, coverage and cppcheck counts are the
fixture's. The one exception is `size` on history builds, marked synthetic in
demo/README.md.
"""
import copy
import json
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEMO_REPOSITORY = "siliconflow-demo/firmware"
DEMO_REPOSITORY_ID = 900000001
DEMO_PROJECT_SLUG = "demo-firmware"
DEMO_PROJECT_NAME = "Demo firmware (replayed inputs)"
HIL_WORKFLOW = ".github/workflows/hil.yml"

NAME_RE = r"^[a-z0-9_]{1,64}$"
FILE_RE = r"^[A-Za-z0-9._-]{1,128}$"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HilRun(Model):
    """A hardware-in-the-loop run that reports on the build (docs/hil.md)."""

    run_id: int = Field(ge=1)
    fixture: str = Field(pattern=FILE_RE)


class ScenarioBuild(Model):
    key: str = Field(pattern=NAME_RE)
    workflow: str = ".github/workflows/firmware-ci.yml"
    job: str = Field(default="host-cmake", pattern=FILE_RE)
    run_id: int = Field(ge=1)
    attempt: int = Field(default=1, ge=1)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    branch: str = "main"
    message: str
    author: str = "demo-developer"
    fixture: str = Field(pattern=FILE_RE)
    # The artifact zip, if it is not <fixture>.zip (e.g. a tampered copy).
    artifact: str | None = Field(default=None, pattern=FILE_RE)
    # demo/fixtures/gates/<gates>: the build commit's .siliconflow/gates.yml.
    gates: str = Field(default="gates.yml", pattern=FILE_RE)
    conclusion: Literal["success", "failure"] = "success"
    # When the run happened, for history builds.
    days_ago: float = Field(default=0, ge=0, le=365)
    # Synthetic ELF totals for builds whose fixture has no size (history only).
    size: dict[Literal["text_bytes", "data_bytes", "bss_bytes"], int] | None = None
    # How many times the completed webhook is delivered, with one X-GitHub-Delivery.
    deliveries: int = Field(default=1, ge=1, le=5)
    hil: HilRun | None = None

    @property
    def ref(self) -> str:
        return f"refs/heads/{self.branch}"

    @property
    def zip_name(self) -> str:
        return self.artifact or self.fixture

    @property
    def artifact_name(self) -> str:
        """The Actions artifact name ci/post_report.sh gives the job's out/ directory."""
        return f"sfci-out-{self.job}-attempt-{self.attempt}"


class Scenario(Model):
    name: str = Field(pattern=NAME_RE)
    title: str
    summary: str
    builds: list[ScenarioBuild] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _unique_keys(self) -> "Scenario":
        if len({b.key for b in self.builds}) != len(self.builds):
            raise ValueError("duplicate build key")
        return self


class DemoFixtures:
    """The demo directory: scenarios and the fixtures they name."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.scenarios: dict[str, Scenario] = {}
        for path in sorted((self.root / "scenarios").glob("*.yml")):
            scenario = Scenario.model_validate(yaml.safe_load(path.read_text()))
            if scenario.name != path.stem:
                raise ValueError(f"{path.name}: name is {scenario.name!r}")
            self.scenarios[scenario.name] = scenario
        self._check_runs()

    def _check_runs(self) -> None:
        """A run id means one run everywhere: the worker's source finds a
        run's zip by it, whichever scenario sent the run."""
        runs: dict[tuple[int, int], ScenarioBuild] = {}
        gates: dict[str, str] = {}
        for scenario in self.scenarios.values():
            for build in scenario.builds:
                seen = runs.setdefault((build.run_id, build.attempt), build)
                if seen != build:
                    raise ValueError(f"run {build.run_id} attempt {build.attempt} is defined twice")
                if gates.setdefault(build.sha, build.gates) != build.gates:
                    raise ValueError(f"commit {build.sha[:12]} has two gates.yml files")

    def builds(self) -> list[ScenarioBuild]:
        return [b for s in self.scenarios.values() for b in s.builds]

    def report(self, fixture: str) -> dict[str, Any]:
        return copy.deepcopy(_read_json(self.root / "fixtures" / "reports" / f"{fixture}.json"))

    def github_event(self, name: str) -> dict[str, Any]:
        return copy.deepcopy(_read_json(self.root / "fixtures" / "github" / name))

    def zip_path(self, name: str) -> Path:
        return self.root / "fixtures" / "artifacts" / f"{name}.zip"

    def gates_path(self, name: str) -> Path:
        return self.root / "fixtures" / "gates" / name


@cache
def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _ts(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def run_report(fixtures: DemoFixtures, build: ScenarioBuild, start: datetime) -> dict[str, Any]:
    """The fixture's run report, as the scenario's run would have posted it
    had it started at `start`."""
    body = fixtures.report(build.fixture)
    body["github"] = {
        "repository": DEMO_REPOSITORY,
        "run_id": build.run_id,
        "run_attempt": build.attempt,
        "sha": build.sha,
        "ref": build.ref,
        "job": build.job,
        "workflow_ref": f"{DEMO_REPOSITORY}/{build.workflow}@{build.ref}",
    }
    end = _shift_stages(body, start)
    body["generated_at"] = _ts(end)
    if body.get("artifact_name") is not None:
        body["artifact_name"] = build.artifact_name
    if build.size is not None:
        body["size"] = {"file": "firmware", **build.size, "sections": []}
    return body


def hil_report(
    fixtures: DemoFixtures, build: ScenarioBuild, build_id: str, start: datetime
) -> dict[str, Any]:
    """The HIL fixture's linked report for `build_id`. hil.yml runs from the
    default branch, so its token's sha is the branch head: the build's commit."""
    assert build.hil is not None
    body = fixtures.report(build.hil.fixture)
    body["github"] = {
        "repository": DEMO_REPOSITORY,
        "run_id": build.hil.run_id,
        "run_attempt": 1,
        "sha": build.sha,
        "ref": "refs/heads/main",
        "job": "hil",
        "workflow_ref": f"{DEMO_REPOSITORY}/{HIL_WORKFLOW}@refs/heads/main",
    }
    body["build_id"] = build_id
    body["generated_at"] = _ts(_shift_stages(body, start))
    return body


def _shift_stages(body: dict[str, Any], start: datetime) -> datetime:
    """Move the stage timestamps so the first stage starts at `start`,
    keeping every duration. Returns when the last stage finished."""
    times = [_parse(s[k]) for s in body["stages"] for k in ("started_at", "finished_at") if s.get(k)]
    if not times:
        return start
    delta = start - min(times)
    for stage in body["stages"]:
        for k in ("started_at", "finished_at"):
            if stage.get(k):
                stage[k] = _ts(_parse(stage[k]) + delta)
    return max(times) + delta


def report_end(report: dict[str, Any], start: datetime) -> datetime:
    """When the report's last stage finished (its start if it has none)."""
    ends = [_parse(s["finished_at"]) for s in report["stages"] if s.get("finished_at")]
    return max(ends, default=start)


def first_line(text: str) -> str:
    return (text.strip().splitlines() or [""])[0][:200]


def workflow_run(
    fixtures: DemoFixtures,
    build: ScenarioBuild,
    action: Literal["requested", "in_progress", "completed"],
    *,
    started: datetime,
    updated: datetime,
) -> dict[str, Any]:
    """A workflow_run delivery, from GitHub's recorded example payloads with
    the scenario run's fields filled in (as tests/github.py does)."""
    name = "workflow_run.completed.json" if action == "completed" else "workflow_run.requested.json"
    payload = fixtures.github_event(name)
    payload["action"] = action
    _demo_repository(payload)
    payload["workflow_run"].update(
        id=build.run_id,
        run_attempt=build.attempt,
        event="push",
        path=build.workflow,
        display_title=first_line(build.message),
        head_branch=build.branch,
        head_sha=build.sha,
        head_commit={
            **(payload["workflow_run"].get("head_commit") or {}),
            "id": build.sha,
            "message": build.message,
            "author": {"name": build.author, "email": f"{build.author}@example.invalid"},
        },
        actor={**payload["workflow_run"]["actor"], "login": build.author},
        triggering_actor={**payload["workflow_run"]["actor"], "login": build.author},
        pull_requests=[],
        created_at=_ts(started),
        run_started_at=_ts(started),
        updated_at=_ts(updated),
        status={"requested": "queued", "in_progress": "in_progress", "completed": "completed"}[action],
        conclusion=build.conclusion if action == "completed" else None,
    )
    return payload


def workflow_job(fixtures: DemoFixtures, build: ScenarioBuild, *, started: datetime) -> dict[str, Any]:
    payload = fixtures.github_event("workflow_job.in_progress.json")
    _demo_repository(payload)
    payload["workflow_job"].update(
        run_id=build.run_id,
        run_attempt=build.attempt,
        head_sha=build.sha,
        head_branch=build.branch,
        name=build.job,
        started_at=_ts(started),
        created_at=_ts(started),
    )
    return payload


def _demo_repository(payload: dict[str, Any]) -> None:
    owner, name = DEMO_REPOSITORY.split("/")
    payload["repository"].update(
        id=DEMO_REPOSITORY_ID, full_name=DEMO_REPOSITORY, name=name, default_branch="main"
    )
    payload["repository"]["owner"] = {**payload["repository"]["owner"], "login": owner}


def at(days_ago: float, now: datetime) -> datetime:
    return (now - timedelta(days=days_ago)).replace(microsecond=0)
