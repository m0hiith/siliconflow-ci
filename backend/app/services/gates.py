"""Release gates: decide whether a passed build's artifacts may be released.

The policy is .siliconflow/gates.yml in the repository at the build's commit,
fetched through the GitHub App. After a build passes and its artifacts have
been ingested, a worker job (EVALUATE_JOB_KIND) evaluates every check and
writes one gate_results row each:

  artifacts_verified    the artifact set was downloaded and re-hashed
  stage:<name>          a stage listed in required_stages passed
  test_failures         failed + errored JUnit cases <= threshold
  line_coverage_min     reported line coverage >= threshold
  analysis_errors_max   cppcheck errors <= threshold
  text_bytes_max        largest ELF's text size <= the limit for the build's profile
  release_ref           the build's ref matches a release_refs pattern

The build's artifacts become release_eligible only if every row passed. Gates
fail closed: missing data fails its rule, and a missing or invalid gates.yml
writes a single failed gates_config row. Pull request builds are never
eligible, since their gates.yml is the PR author's to edit. Builds that did not
pass are never evaluated, so they are never eligible. A run report has no way
to set eligibility; its schema rejects any field it does not define.

Results are written once per build. The job is queued when the build passes
and, if the build had already passed, when ingestion finishes. It does its
work only once both have happened.
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    Artifact,
    AuditLog,
    Build,
    BuildStage,
    BuildStatus,
    BuildTrigger,
    GateResult,
    Job,
    Repository,
    RunReport,
    StageStatus,
    TestCase,
    TestOutcome,
)
from app.services.artifacts import repo_installation_id, storage_path
from app.services.elf import ElfError, text_bytes
from app.services.github import GitHubApp

log = logging.getLogger(__name__)

EVALUATE_JOB_KIND = "evaluate_gates"
GATES_PATH = ".siliconflow/gates.yml"
MAX_CONFIG_BYTES = 64 * 1024
MAX_MESSAGE_LEN = 2000

# ------------------------------------------------------------------ config


def _ref_glob(value: str) -> str:
    if not value.startswith("refs/") or any(c.isspace() for c in value):
        raise ValueError("must be a full ref pattern such as 'refs/heads/main' or 'refs/tags/v*'")
    return value


StageName = Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")]
RefGlob = Annotated[str, Field(max_length=255), AfterValidator(_ref_glob)]


class GatesModel(BaseModel):
    # Strict: a YAML `yes` is not a threshold of 1, and a typo is an error.
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Rule(GatesModel):
    # The rule fails unless this stage passed: its numbers mean nothing otherwise.
    requires_stage: StageName | None = None

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, value: Any) -> Any:
        # `test_failures: 0` is short for `test_failures: {threshold: 0}`.
        return value if isinstance(value, dict) else {"threshold": value}


class CountMax(Rule):
    threshold: int = Field(ge=0)


class PercentMin(Rule):
    threshold: float = Field(ge=0, le=100)


class TextBytesMax(Rule):
    threshold: int = Field(ge=0)
    # Only builds of this profile are checked by this limit.
    profile: str | None = Field(default=None, min_length=1, max_length=100)


class Rules(GatesModel):
    test_failures: CountMax | None = None
    line_coverage_min: PercentMin | None = None
    analysis_errors_max: CountMax | None = None
    # One limit, or a list of limits with one per profile (and at most one
    # without a profile, for every other profile). See text_limit_for().
    text_bytes_max: Annotated[list[TextBytesMax], Field(min_length=1, max_length=32)] | None = None

    @field_validator("text_bytes_max", mode="before")
    @classmethod
    def _one_limit_is_a_list_of_one(cls, value: Any) -> Any:
        return value if value is None or isinstance(value, list) else [value]

    @field_validator("text_bytes_max")
    @classmethod
    def _one_limit_per_profile(cls, limits: list[TextBytesMax] | None) -> list[TextBytesMax] | None:
        profiles = [r.profile for r in limits or []]
        if len(set(profiles)) != len(profiles):
            raise ValueError("more than one limit for the same profile (or without a profile)")
        return limits


class GatesConfig(GatesModel):
    version: Literal[1]
    required_stages: list[StageName] = Field(default_factory=list, max_length=64)
    rules: Rules = Field(default_factory=Rules)
    # No pattern means no ref is a release ref, so nothing is ever eligible.
    release_refs: list[RefGlob] = Field(default_factory=list, max_length=64)


class GatesConfigError(ValueError):
    pass


def parse_config(raw: bytes) -> GatesConfig:
    if len(raw) > MAX_CONFIG_BYTES:
        raise GatesConfigError(f"{GATES_PATH} is larger than {MAX_CONFIG_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
        # Anchors and aliases are refused: a few lines of them can expand
        # into a document far too large to validate.
        if any(isinstance(e, yaml.AliasEvent) for e in yaml.parse(text, Loader=yaml.SafeLoader)):
            raise GatesConfigError(f"{GATES_PATH}: YAML aliases are not allowed")
        data = yaml.safe_load(text)
    except UnicodeDecodeError as exc:
        raise GatesConfigError(f"{GATES_PATH} is not UTF-8") from exc
    except yaml.YAMLError as exc:
        raise GatesConfigError(f"{GATES_PATH} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise GatesConfigError(f"{GATES_PATH} must be a mapping")
    try:
        return GatesConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(top level)'}: {e['msg']}" for e in exc.errors()
        )
        raise GatesConfigError(f"{GATES_PATH} is invalid: {problems}") from exc


@cache
def ref_regex(pattern: str) -> re.Pattern[str]:
    """`*` and `?` stay within one path segment; `**` crosses segments."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        c = pattern[i]
        out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.compile("".join(out))


def ref_matches(ref: str, patterns: list[str]) -> bool:
    return any(ref_regex(p).fullmatch(ref) for p in patterns)


# -------------------------------------------------------------- evaluation


@dataclass(frozen=True)
class Facts:
    """What the backend knows about a passed build. A None measurement is
    missing; `missing` says why, keyed by rule id."""

    ref: str
    trigger: BuildTrigger
    profile: str
    stages: dict[str, StageStatus]
    artifacts_state: str
    artifact_count: int
    test_failures: int | None = None
    line_coverage: float | None = None
    analysis_errors: int | None = None
    text_bytes: int | None = None
    text_file: str | None = None
    missing: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    rule_id: str
    passed: bool
    observed: Any
    threshold: Any
    message: str


def _stage_message(name: str, facts: Facts) -> str | None:
    status = facts.stages.get(name)
    if status is StageStatus.passed:
        return None
    return f"stage {name} {'was not reported' if status is None else status.value}"


def _rule(
    rule_id: str,
    rule: Rule,
    facts: Facts,
    observed: Any,
    ok: bool,
    passed_msg: str,
    failed_msg: str,
) -> Result:
    if rule.requires_stage and (problem := _stage_message(rule.requires_stage, facts)):
        return Result(rule_id, False, observed, rule.threshold, f"requires stage {rule.requires_stage}: {problem}")
    if observed is None:
        return Result(rule_id, False, None, rule.threshold, facts.missing.get(rule_id, "not measured"))
    return Result(rule_id, ok, observed, rule.threshold, passed_msg if ok else failed_msg)


def text_limit_for(limits: list[TextBytesMax], profile: str) -> TextBytesMax | None:
    """The limit naming this profile, else the one naming no profile."""
    return next((r for r in limits if r.profile == profile), None) or next(
        (r for r in limits if r.profile is None), None
    )


def evaluate(config: GatesConfig, facts: Facts) -> list[Result]:
    """Every check for a passed build, in a stable order. Pure, for testing."""
    results = []

    verified = facts.artifacts_state == "stored" and facts.artifact_count > 0
    results.append(
        Result(
            "artifacts_verified",
            verified,
            {"state": facts.artifacts_state, "count": facts.artifact_count},
            {"state": "stored", "count_min": 1},
            f"{facts.artifact_count} artifact(s) verified and stored"
            if verified
            else f"artifacts are {facts.artifacts_state} with {facts.artifact_count} stored",
        )
    )

    for name in config.required_stages:
        problem = _stage_message(name, facts)
        status = facts.stages.get(name)
        results.append(
            Result(
                f"stage:{name}",
                problem is None,
                None if status is None else status.value,
                StageStatus.passed.value,
                problem or f"stage {name} passed",
            )
        )

    rules = config.rules
    if r := rules.test_failures:
        n = facts.test_failures
        results.append(
            _rule("test_failures", r, facts, n, n is not None and n <= r.threshold,
                  f"{n} failing test(s), at most {r.threshold} allowed",
                  f"{n} failing test(s), more than the {r.threshold} allowed")
        )
    if r := rules.line_coverage_min:
        pct = facts.line_coverage
        results.append(
            _rule("line_coverage_min", r, facts, pct, pct is not None and pct >= r.threshold,
                  f"line coverage {pct}% meets the {r.threshold}% minimum",
                  f"line coverage {pct}% is below the {r.threshold}% minimum")
        )
    if r := rules.analysis_errors_max:
        n = facts.analysis_errors
        results.append(
            _rule("analysis_errors_max", r, facts, n, n is not None and n <= r.threshold,
                  f"{n} analysis error(s), at most {r.threshold} allowed",
                  f"{n} analysis error(s), more than the {r.threshold} allowed")
        )
    if limits := rules.text_bytes_max:
        if (r := text_limit_for(limits, facts.profile)) is None:
            profiles = ", ".join(str(r.profile) for r in limits)
            threshold = limits[0].threshold if len(limits) == 1 else None
            results.append(
                Result("text_bytes_max", True, None, threshold,
                       f"applies to profile {profiles} only; this build is {facts.profile}")
            )
        else:
            n = facts.text_bytes
            results.append(
                _rule("text_bytes_max", r, facts, n, n is not None and n <= r.threshold,
                      f"{facts.text_file}: {n} text bytes, within the {r.threshold}-byte limit",
                      f"{facts.text_file}: {n} text bytes, over the {r.threshold}-byte limit")
            )

    if facts.trigger is BuildTrigger.pull_request:
        ref_ok, ref_msg = False, "pull request builds are never release-eligible"
    elif ref_matches(facts.ref, config.release_refs):
        ref_ok, ref_msg = True, f"{facts.ref} is a release ref"
    else:
        ref_ok, ref_msg = False, f"{facts.ref} matches no release_refs pattern"
    results.append(Result("release_ref", ref_ok, facts.ref, list(config.release_refs), ref_msg))
    return results


# ---------------------------------------------------------------- database


async def queue_evaluation(session: AsyncSession, build_id: str) -> None:
    await session.execute(insert(Job).values(kind=EVALUATE_JOB_KIND, payload={"build_id": build_id}))


async def queue_if_passed(session: AsyncSession, build_id: str) -> None:
    """After ingestion: queue the evaluation if the build has already passed.

    FOR SHARE makes this race-free against outcome.settle. If settle is moving
    the build to passed, this waits for it and sees passed. If this runs first,
    settle's UPDATE waits for ingestion to commit, so the job settle queues
    sees the stored artifacts.
    """
    status = await session.scalar(
        select(Build.status).where(Build.id == build_id).with_for_update(read=True)
    )
    if status is BuildStatus.passed:
        await queue_evaluation(session, build_id)


async def load_config(
    session: AsyncSession, github: GitHubApp | None, build: Build
) -> GatesConfig:
    """The build's gates.yml. Raises GatesConfigError if it cannot be used;
    a GitHub outage raises GitHubError instead, so the job retries."""
    if build.commit_sha is None:
        raise GatesConfigError("the build has no commit to read gates.yml from")
    if github is None:
        raise RuntimeError("no GitHub client in this worker")
    repo = await session.get_one(Repository, build.repository_id)
    installation_id = await repo_installation_id(session, github, repo)
    raw = await github.get_file(installation_id, repo.full_name, GATES_PATH, build.commit_sha)
    if raw is None:
        raise GatesConfigError(f"no {GATES_PATH} at commit {build.commit_sha[:12]}")
    return parse_config(raw)


def _largest_text(root: Path, elves: list[tuple[str, str]]) -> tuple[int | None, str | None, str | None]:
    """(text bytes, file name, problem) for the largest ELF. Blocking."""
    best: tuple[int, str] | None = None
    for file_name, key in elves:
        try:
            n = text_bytes(storage_path(root, key))
        except (ElfError, OSError) as exc:
            return None, file_name, f"{file_name}: cannot read its sections ({exc})"
        if best is None or n > best[0]:
            best = (n, file_name)
    if best is None:
        return None, None, "no ELF artifact to measure"
    return best[0], best[1], None


async def gather_facts(
    session: AsyncSession, settings: Settings, build: Build, report: RunReport
) -> Facts:
    stages = dict(
        (await session.execute(
            select(BuildStage.name, BuildStage.status).where(BuildStage.build_id == build.id)
        )).all()
    )
    artifacts = (
        await session.execute(
            select(Artifact.file_name, Artifact.kind, Artifact.storage_key)
            .where(Artifact.build_id == build.id)
            .order_by(Artifact.file_name)
        )
    ).all()
    missing: dict[str, str] = {}
    payload = report.payload

    # Test results count only if their JUnit files were verified and all parsed.
    test_failures = None
    if report.artifacts_state != "stored":
        missing["test_failures"] = f"no verified test results (artifacts are {report.artifacts_state})"
    elif not payload.get("junit"):
        missing["test_failures"] = "the run report listed no JUnit files"
    elif report.artifacts_detail:
        missing["test_failures"] = f"JUnit files could not all be parsed: {report.artifacts_detail}"
    else:
        counts = dict(
            (await session.execute(
                select(TestCase.outcome, func.count())
                .where(TestCase.build_id == build.id)
                .group_by(TestCase.outcome)
            )).all()
        )
        if not counts:
            missing["test_failures"] = "no test cases were recorded"
        else:
            test_failures = counts.get(TestOutcome.failed, 0) + counts.get(TestOutcome.error, 0)

    coverage = (payload.get("coverage") or {}).get("line_percent")
    if coverage is None:
        missing["line_coverage_min"] = "the run report has no line coverage"
    errors = (payload.get("cppcheck") or {}).get("error")
    if errors is None:
        missing["analysis_errors_max"] = "the run report has no cppcheck error count"

    elves = [(name, key) for name, kind, key in artifacts if kind == "elf" and key]
    text, text_file, problem = await asyncio.to_thread(
        _largest_text, Path(settings.artifact_root), elves
    )
    if problem:
        missing["text_bytes_max"] = problem

    return Facts(
        ref=build.ref,
        trigger=build.trigger,
        profile=build.profile,
        stages=stages,
        artifacts_state=report.artifacts_state,
        artifact_count=len(artifacts),
        test_failures=test_failures,
        line_coverage=coverage,
        analysis_errors=errors,
        text_bytes=text,
        text_file=text_file,
        missing=missing,
    )


async def evaluate_build(
    session: AsyncSession, github: GitHubApp | None, settings: Settings, build_id: str
) -> None:
    """The EVALUATE_JOB_KIND job. Idempotent; the caller commits."""
    # The row lock serialises the two jobs a build can get.
    build = await session.scalar(select(Build).where(Build.id == build_id).with_for_update())
    if build is None or build.status is not BuildStatus.passed:
        return
    report = await session.scalar(select(RunReport).where(RunReport.build_id == build_id))
    if report is None or report.artifacts_state == "pending":
        return  # ingestion queues this job again when it finishes
    if await session.scalar(select(exists().where(GateResult.build_id == build_id))):
        return

    try:
        config = await load_config(session, github, build)
    except GatesConfigError as exc:
        results = [Result("gates_config", False, None, None, str(exc))]
    else:
        results = evaluate(config, await gather_facts(session, settings, build, report))

    await session.execute(
        insert(GateResult),
        [
            {
                "build_id": build_id,
                "rule_id": r.rule_id,
                "passed": r.passed,
                "observed": r.observed,
                "threshold": r.threshold,
                "message": r.message[:MAX_MESSAGE_LEN],
            }
            for r in results
        ],
    )
    eligible = all(r.passed for r in results)
    if eligible:
        await session.execute(
            update(Artifact).where(Artifact.build_id == build_id).values(release_eligible=True)
        )
    failed = [r.rule_id for r in results if not r.passed]
    session.add(
        AuditLog(
            action="gates.evaluated",
            target_type="build",
            target_id=build_id,
            details={"release_eligible": eligible, "failed": failed},
        )
    )
    await session.flush()
    log.info("gates evaluated", extra={"build_id": build_id, "eligible": eligible, "failed": failed})
