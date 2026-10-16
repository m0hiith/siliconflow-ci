"""API request and response models."""
from datetime import date, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models import BuildStatus, BuildTrigger, StageStatus, TestOutcome


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RepositoryOut(ORMModel):
    id: str
    full_name: str
    default_branch: str


class ProjectOut(ORMModel):
    id: str
    slug: str
    name: str
    default_branch: str
    created_at: datetime
    repositories: list[RepositoryOut]


class BuildOut(ORMModel):
    id: str
    project_id: str
    repository_id: str
    number: int
    status: BuildStatus
    trigger: BuildTrigger
    profile: str
    ref: str
    branch: str | None
    commit_sha: str | None
    commit_message: str | None
    author_login: str | None
    pr_number: int | None
    gh_run_id: int | None
    gh_run_attempt: int | None
    status_reason: str | None
    failure_class: str | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class BuildDetailOut(BuildOut):
    """A build, with what its details page needs beyond the list row."""

    repository: str = Field(description="owner/name of the build's repository")
    commit_url: str | None
    run_url: str | None = Field(description="The GitHub Actions run attempt")
    toolchain: dict[str, str | None] = Field(
        description="Tool versions from the run report (empty without one)"
    )
    sdk: dict[str, str | None] = Field(
        description="SDK versions from the run report, e.g. simplicity_sdk and wiseconnect "
        "(empty without one, or for profiles that use no vendor SDK)"
    )
    artifacts_state: str | None = Field(
        description="Artifact ingestion: none, pending, stored, rejected or unavailable; "
        "null without a run report"
    )


class BuildCreate(BaseModel):
    """A manual build. Defaults: the project's only repository, and that
    repository's default branch."""

    repository_id: str | None = None
    # A branch name, not a full ref; git's forbidden characters are rejected.
    branch: str | None = Field(
        default=None, min_length=1, max_length=255, pattern=r"^[^\s~^:?*\[\\]+$"
    )

    @field_validator("branch")
    @classmethod
    def _plain_branch_name(cls, value: str | None) -> str | None:
        if value is not None and (value.startswith(("refs/", "-", "/")) or ".." in value):
            raise ValueError("expected a branch name such as 'main', not a ref")
        return value


class BuildPage(BaseModel):
    items: list[BuildOut]
    next_cursor: str | None


class StageOut(ORMModel):
    name: str
    position: int
    required: bool
    status: StageStatus
    exit_code: int | None
    started_at: datetime | None
    finished_at: datetime | None
    failure_class: str | None = Field(
        default=None,
        description="Why the stage failed, from its log or (exit code 75: infra_hw) its exit code; "
        "null when unclassified",
    )


class FirstErrorOut(BaseModel):
    line_number: int = Field(description="Line in the log, 1-based")
    message: str
    file: str | None = Field(description="Source file as the tool printed it")
    line: int | None
    column: int | None
    path: str | None = Field(
        default=None, description="file relative to the repository root, when known"
    )
    url: str | None = Field(default=None, description="The file at the build's commit on GitHub")


class StageLogOut(BaseModel):
    """A page of a stage's log, plus the index of the whole log. Line numbers
    are 1-based; `lines[i]` is line `offset + i + 1`."""

    stage: str
    offset: int
    total_lines: int
    lines: list[str]
    error_count: int
    warning_count: int
    errors: list[int] = Field(description="Error line numbers (at most 1000)")
    warnings: list[int] = Field(description="Warning line numbers (at most 1000)")
    failure_class: str | None
    first_error: FirstErrorOut | None


class LogSearchOut(BaseModel):
    stage: str
    query: str
    total: int = Field(description="Matching lines in the whole log")
    matches: list[int] = Field(description="Matching line numbers, 1-based (at most `limit`)")


class PreviousPassingOut(BaseModel):
    """The last passing build before this one on the same repository, ref and
    profile; null when there is none."""

    build: BuildOut | None
    compare_url: str | None = Field(
        description="GitHub compare view from that build's commit to this one's"
    )
    commits_between: int | None = Field(
        default=None,
        description="Commits in this build that the passing build lacks, from GitHub; "
        "null when unknown (no GitHub App installation, or GitHub did not answer)",
    )


class GateResultOut(ORMModel):
    rule_id: str
    passed: bool
    observed: Any
    threshold: Any
    message: str | None
    evaluated_at: datetime


class TestCaseOut(ORMModel):
    __test__ = False  # not a pytest test class

    suite: str
    name: str
    classname: str | None
    outcome: TestOutcome
    duration_ms: int | None
    message: str | None


class ArtifactOut(ORMModel):
    id: str
    kind: str
    file_name: str
    size_bytes: int
    sha256: str
    release_eligible: bool
    created_at: datetime


class SizeSectionOut(BaseModel):
    name: str
    size: int
    addr: int


class SizeTotalsOut(ORMModel):
    file_name: str = Field(description="The ELF that was measured")
    text_bytes: int
    data_bytes: int
    bss_bytes: int


class BuildSizeOut(SizeTotalsOut):
    """An ELF's size as the build's run report stated it (ci/size.py, GNU size)."""

    sections: list[SizeSectionOut] = Field(description="Sections as `size -A` lists them")


class BuildSizeCompareOut(BaseModel):
    """A build's size, and the size of the build it is compared with: the
    latest passing build before it on the repository's default branch, with
    the same profile and a reported size."""

    size: BuildSizeOut | None = Field(description="null when the run report had no size")
    base_branch: str
    base_build: BuildOut | None
    base_size: BuildSizeOut | None


class SizePointOut(SizeTotalsOut):
    """One build's reported size, for size history."""

    build_id: str
    build_number: int
    status: BuildStatus
    profile: str
    branch: str | None
    commit_sha: str | None
    created_at: datetime


class MetricsSummaryOut(BaseModel):
    """Build health for the current UTC day (see GET /metrics/summary)."""

    generated_at: datetime
    window_start: datetime = Field(description="Start of the current UTC day")
    builds_today: int = Field(description="Builds created since window_start")
    status_counts: dict[str, int] = Field(
        description="Builds created since window_start, by status (every status present)"
    )
    failed_today: int
    pass_rate: float | None = Field(
        description="passed / (passed + failed) today; null when neither occurred"
    )
    median_duration_seconds: float | None = Field(
        description="Median started-to-finished time of today's passed and failed builds"
    )
    active_builds: int = Field(description="Queued or running builds, whenever created")
    recent_failures: list[BuildOut] = Field(description="Latest failed builds, any day")


# --------------------------------------------------------------- analytics
# Every analytics response states how many builds it was computed from. Below
# min_builds it is not enough data: enough_data is false and the series are
# empty, so nothing is drawn from a handful of points.


class AnalyticsWindow(BaseModel):
    generated_at: datetime
    window_start: datetime = Field(description="Builds created since then are considered")
    builds: int = Field(description="Builds the analysis was computed from")
    min_builds: int
    enough_data: bool = Field(description="builds >= min_builds; series are empty otherwise")


class DurationDayOut(BaseModel):
    profile: str
    day: date = Field(description="UTC day the builds were created on")
    builds: int
    median_seconds: float
    p90_seconds: float


class DurationsOut(AnalyticsWindow):
    """Started-to-finished time of passed and failed builds, per profile and
    UTC day. Days without such a build have no point."""

    days: list[DurationDayOut]


class FailureClassCountOut(BaseModel):
    failure_class: str | None = Field(description="null when the build was not classified")
    count: int


class PassRateWeekOut(BaseModel):
    week_start: date = Field(description="Monday (UTC) of the ISO week")
    builds: int = Field(description="Terminal builds created that week")
    status_counts: dict[str, int] = Field(description="By terminal status (every one present)")
    pass_rate: float | None = Field(
        description="passed / (passed + failed); null when neither occurred"
    )
    failure_classes: list[FailureClassCountOut] = Field(
        description="failure_class of the week's failed, timed_out and errored builds, "
        "most frequent first"
    )


class PassRateOut(AnalyticsWindow):
    """Terminal builds per ISO week. Weeks without one have no entry."""

    weeks: list[PassRateWeekOut]


class SizeCommitOut(BaseModel):
    """The size of one commit: its latest build on the default branch that
    reported one. flash = text + data (code plus initialised data's load
    image); ram = data + bss."""

    build_id: str
    build_number: int
    status: BuildStatus
    commit_sha: str
    created_at: datetime
    text_bytes: int
    data_bytes: int
    bss_bytes: int
    flash_bytes: int
    ram_bytes: int


class SizeSeriesOut(BaseModel):
    profile: str
    builds: int = Field(description="Default-branch builds of this profile that reported a size")
    enough_data: bool
    commits: list[SizeCommitOut] = Field(description="Oldest first")


class SizeTrendsOut(BaseModel):
    generated_at: datetime
    min_builds: int
    series: list[SizeSeriesOut] = Field(description="One per requested profile, in request order")


class FlakyExampleOut(BaseModel):
    build_id: str
    build_number: int
    commit_sha: str | None
    outcome: TestOutcome
    created_at: datetime


class FlakyTestOut(BaseModel):
    repository: str
    profile: str
    suite: str
    name: str
    same_commit_shas: list[str] = Field(
        description="Commits on which the test both passed and failed (newest first)"
    )
    flips: int | None = Field(
        description="Pass/fail changes over the default branch's recent builds; "
        "set only when above the threshold"
    )
    examples: list[FlakyExampleOut] = Field(
        description="Builds showing it: a pass and a failure per commit, then the builds "
        "where it flipped; newest first"
    )


class FlakyTestsOut(AnalyticsWindow):
    """Tests that passed and failed on the same commit since window_start, or
    flipped more than flip_threshold times over the last recent_builds
    terminal default-branch builds of their profile. error counts as failed;
    skipped is ignored."""

    recent_builds: int
    flip_threshold: int
    tests: list[FlakyTestOut]


class HealthOut(BaseModel):
    status: str
    detail: dict[str, Any] = {}


class WebhookAck(BaseModel):
    status: Literal["accepted", "duplicate"]


# -------------------------------------------------------------- run report
# The body CI posts to /api/v1/runs/report (ci/report.py writes it). Every
# field is a claim by the runner; the backend verifies what it can (identity
# via OIDC, files by re-hashing) and decides status itself.

RUN_REPORT_SCHEMA = "sfci.run-report/1"
SHA1_HEX = r"^[0-9a-f]{40}$"
SHA256_HEX = r"^[0-9a-f]{64}$"


def _relative_path(value: str) -> str:
    """A plain relative POSIX path: no absolute paths, '..', '.', or backslashes."""
    parts = PurePosixPath(value).parts
    if (
        not value
        or "\\" in value
        or value.startswith("/")
        or any(part in ("", ".", "..") for part in parts)
        or value != "/".join(parts)
    ):
        raise ValueError("must be a relative path without '.' or '..' segments")
    return value


RelativePath = Annotated[str, Field(max_length=512), AfterValidator(_relative_path)]


class ReportModel(BaseModel):
    # A new field is a new schema version, so unknown fields are rejected.
    model_config = ConfigDict(extra="forbid")


class ReportGitHub(ReportModel):
    repository: str = Field(max_length=200)
    run_id: int = Field(ge=1)
    run_attempt: int = Field(ge=1)
    sha: str = Field(pattern=SHA1_HEX)
    ref: str | None = Field(default=None, max_length=500)
    job: str | None = Field(default=None, max_length=200)
    workflow_ref: str | None = Field(default=None, max_length=500)


class ReportStage(ReportModel):
    name: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    status: Literal["passed", "failed", "skipped"]
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    required: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> "ReportStage":
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        return self


class ReportCoverage(ReportModel):
    line_percent: float = Field(ge=0, le=100)
    lines_covered: int | None = Field(default=None, ge=0)
    lines_valid: int | None = Field(default=None, ge=0)


class ReportFile(ReportModel):
    """A JUnit file, relative to the out directory (= the artifact zip root)."""

    path: RelativePath
    size: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256_HEX)


class ManifestFile(ReportModel):
    """An artifact, relative to out/artifacts."""

    file: RelativePath
    size: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256_HEX)


class ArtifactManifest(ReportModel):
    schema_: Literal["sfci.artifact-manifest/1"] = Field(alias="schema")
    files: list[ManifestFile] = Field(max_length=1000)

    @field_validator("files")
    @classmethod
    def _unique(cls, files: list[ManifestFile]) -> list[ManifestFile]:
        if len({f.file for f in files}) != len(files):
            raise ValueError("duplicate file in manifest")
        return files


# Bytes; far beyond any firmware image, and within BIGINT.
SizeBytes = Annotated[int, Field(strict=True, ge=0, le=1 << 40)]


class ReportSection(ReportModel):
    name: str = Field(min_length=1, max_length=200)
    size: SizeBytes
    addr: SizeBytes


class ReportSize(ReportModel):
    """An ELF's footprint as ci/size.py measured it with GNU size: Berkeley
    text/data/bss totals and the `size -A` section list."""

    file: str = Field(pattern=r"^[A-Za-z0-9._-]{1,255}$")
    text_bytes: SizeBytes
    data_bytes: SizeBytes
    bss_bytes: SizeBytes
    sections: list[ReportSection] = Field(default_factory=list, max_length=256)


class RunReportIn(ReportModel):
    schema_: Literal["sfci.run-report/1"] = Field(alias="schema")
    generated_at: datetime
    github: ReportGitHub
    stages: list[ReportStage] = Field(max_length=64)
    toolchain: dict[str, str | None] = Field(default_factory=dict, max_length=32)
    # Vendor SDK versions the toolchain image declares (ci/report.py --versions).
    # Optional, so reports from runners that predate it stay valid.
    sdk: dict[str, str | None] | None = Field(default=None, max_length=16)
    coverage: ReportCoverage | None = None
    cppcheck: dict[str, Annotated[int, Field(ge=0)]] | None = Field(default=None, max_length=16)
    size: ReportSize | None = None
    junit: list[ReportFile] = Field(default_factory=list, max_length=64)
    # Name of the Actions artifact holding the zipped out directory.
    artifact_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,128}$")
    manifest: ArtifactManifest | None = None
    # A linked report (services.linked_reports) adds to the build it names
    # instead of describing a build of its own run. Optional, so reports from
    # runners that predate them stay valid; only together.
    profile: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    build_id: str | None = Field(default=None, pattern=r"^bld_[0-9A-Za-z]{1,28}$")

    @field_validator("stages")
    @classmethod
    def _unique_stages(cls, stages: list[ReportStage]) -> list[ReportStage]:
        if len({s.name for s in stages}) != len(stages):
            raise ValueError("duplicate stage name")
        return stages

    @model_validator(mode="after")
    def _linked_fields_together(self) -> "RunReportIn":
        if (self.profile is None) != (self.build_id is None):
            raise ValueError("profile and build_id go together")
        return self

    @model_validator(mode="after")
    def _files_need_an_artifact(self) -> "RunReportIn":
        if (self.manifest is not None or self.junit) and self.artifact_name is None:
            raise ValueError("manifest and junit files need artifact_name")
        return self


class RunReportAck(BaseModel):
    status: Literal["accepted", "duplicate"]
    build_id: str
