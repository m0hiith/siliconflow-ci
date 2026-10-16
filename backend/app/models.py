"""ORM models. The schema itself is owned by Alembic migrations; a test checks
that these models and the migrated database do not drift apart.

Constraint and index names are spelled out so the hand-written migration can
use the same names.
"""
import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.ids import new_id

ID = String(32)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
    )


# ------------------------------------------------------------------- enums


class BuildStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    passed = "passed"
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"
    errored = "errored"


TERMINAL_BUILD_STATUSES = frozenset(
    {
        BuildStatus.passed,
        BuildStatus.failed,
        BuildStatus.cancelled,
        BuildStatus.timed_out,
        BuildStatus.errored,
    }
)


class StageStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    passed = "passed"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"


class BuildTrigger(str, enum.Enum):
    push = "push"
    pull_request = "pull_request"
    manual = "manual"


class TestOutcome(str, enum.Enum):
    __test__ = False  # not a pytest test class

    passed = "passed"
    failed = "failed"
    skipped = "skipped"
    error = "error"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class UserRole(str, enum.Enum):
    viewer = "viewer"
    maintainer = "maintainer"


def pg_enum(py_enum: type[enum.Enum], name: str) -> Enum:
    """A native Postgres enum storing member *values*; the type is created by migrations."""
    return Enum(
        py_enum,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        create_type=False,
        validate_strings=True,
    )


def created_at_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def finished_after_started(table: str) -> CheckConstraint:
    return CheckConstraint(
        "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
        name=f"ck_{table}_finished_after_started",
    )


# ------------------------------------------------------------------ tables


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(ID, primary_key=True, default=lambda: new_id("usr"))
    github_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    login: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    role: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role"), nullable=False, server_default=UserRole.viewer.value
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("github_id", name="uq_users_github_id"),
        UniqueConstraint("login", name="uq_users_login"),
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(ID, primary_key=True, default=lambda: new_id("prj"))
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str] = mapped_column(Text, nullable=False, server_default="main")
    # Next per-project build number; see services/builds.allocate_build_number.
    next_build_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = created_at_column()

    repositories: Mapped[list["Repository"]] = relationship(
        back_populates="project", order_by="Repository.full_name"
    )

    __table_args__ = (
        UniqueConstraint("slug", name="uq_projects_slug"),
        CheckConstraint("next_build_number >= 1", name="ck_projects_next_build_number_positive"),
    )


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[str] = mapped_column(ID, primary_key=True, default=lambda: new_id("rep"))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    full_name: Mapped[str] = mapped_column(Text, nullable=False)  # "owner/name"
    gh_repo_id: Mapped[int | None] = mapped_column(BigInteger)
    gh_installation_id: Mapped[int | None] = mapped_column(BigInteger)
    default_branch: Mapped[str] = mapped_column(Text, nullable=False, server_default="main")
    created_at: Mapped[datetime] = created_at_column()

    project: Mapped[Project] = relationship(back_populates="repositories")

    __table_args__ = (
        UniqueConstraint("full_name", name="uq_repositories_full_name"),
        UniqueConstraint("gh_repo_id", name="uq_repositories_gh_repo_id"),
        Index("ix_repositories_project_id", "project_id"),
    )


class Build(Base):
    __tablename__ = "builds"

    id: Mapped[str] = mapped_column(ID, primary_key=True, default=lambda: new_id("bld"))
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    repository_id: Mapped[str] = mapped_column(
        ForeignKey("repositories.id", ondelete="RESTRICT"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[BuildStatus] = mapped_column(
        pg_enum(BuildStatus, "build_status"),
        nullable=False,
        server_default=BuildStatus.queued.value,
    )
    trigger: Mapped[BuildTrigger] = mapped_column(
        pg_enum(BuildTrigger, "build_trigger"), nullable=False
    )
    profile: Mapped[str] = mapped_column(Text, nullable=False)

    ref: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. refs/heads/main
    branch: Mapped[str | None] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(String(40))
    commit_message: Mapped[str | None] = mapped_column(Text)
    author_login: Mapped[str | None] = mapped_column(Text)
    pr_number: Mapped[int | None] = mapped_column(Integer)

    gh_run_id: Mapped[int | None] = mapped_column(BigInteger)
    gh_run_attempt: Mapped[int | None] = mapped_column(Integer)
    gh_conclusion: Mapped[str | None] = mapped_column(Text)  # raw workflow_run conclusion
    status_reason: Mapped[str | None] = mapped_column(Text)
    failure_class: Mapped[str | None] = mapped_column(Text)

    triggered_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("project_id", "number", name="uq_builds_project_id_number"),
        UniqueConstraint("gh_run_id", "gh_run_attempt", name="uq_builds_gh_run_id_gh_run_attempt"),
        finished_after_started("builds"),
        CheckConstraint("number >= 1", name="ck_builds_number_positive"),
        CheckConstraint(
            "(gh_run_id IS NULL) = (gh_run_attempt IS NULL)", name="ck_builds_gh_run_pair"
        ),
        CheckConstraint(
            "gh_run_attempt IS NULL OR gh_run_attempt >= 1", name="ck_builds_gh_run_attempt_positive"
        ),
        CheckConstraint(
            "commit_sha IS NULL OR commit_sha ~ '^[0-9a-f]{40}$'", name="ck_builds_commit_sha_format"
        ),
        Index("ix_builds_created_at_id", "created_at", "id"),
        Index("ix_builds_project_id_created_at", "project_id", "created_at"),
        Index("ix_builds_status", "status"),
        Index("ix_builds_branch", "branch"),
        Index("ix_builds_commit_sha", "commit_sha"),
    )


class BuildStage(Base):
    __tablename__ = "build_stages"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    build_id: Mapped[str] = mapped_column(
        ForeignKey("builds.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    status: Mapped[StageStatus] = mapped_column(
        pg_enum(StageStatus, "stage_status"),
        nullable=False,
        server_default=StageStatus.pending.value,
    )
    exit_code: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Storage key of the gzipped, redacted log (services.stage_logs).
    log_key: Mapped[str | None] = mapped_column(Text)
    # Error/warning line numbers, failure class and first error (classify.LogIndex).
    log_index: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("build_id", "name", name="uq_build_stages_build_id_name"),
        finished_after_started("build_stages"),
    )

    @property
    def failure_class(self) -> str | None:
        return (self.log_index or {}).get("failure_class")


class TestCase(Base):
    __tablename__ = "test_cases"
    __test__ = False  # not a pytest test class

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    build_id: Mapped[str] = mapped_column(
        ForeignKey("builds.id", ondelete="CASCADE"), nullable=False
    )
    suite: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    classname: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[TestOutcome] = mapped_column(pg_enum(TestOutcome, "test_outcome"), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("build_id", "suite", "name", name="uq_test_cases_build_id_suite_name"),
        Index("ix_test_cases_build_id_outcome", "build_id", "outcome"),
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(ID, primary_key=True, default=lambda: new_id("art"))
    build_id: Mapped[str] = mapped_column(
        ForeignKey("builds.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # elf, bin, map, ...
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(Text)
    # Set only by the gate evaluator (services.gates).
    release_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        UniqueConstraint("build_id", "kind", "sha256", name="uq_artifacts_build_id_kind_sha256"),
        CheckConstraint("size_bytes >= 0", name="ck_artifacts_size_nonnegative"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_artifacts_sha256_format"),
        Index("ix_artifacts_sha256", "sha256"),
    )


class RunReport(Base):
    """The run report a CI job posted (schema sfci.run-report/1), one per build.

    artifacts_state tracks the artifact ingestion job: "none" (nothing to
    fetch), "pending", "stored", "rejected" (a file disagreed with the
    manifest; nothing was stored) or "unavailable" (expired or too large).
    """

    __tablename__ = "run_reports"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    build_id: Mapped[str] = mapped_column(
        ForeignKey("builds.id", ondelete="CASCADE"), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # SHA-256 of the canonical payload; a byte-identical retry is a duplicate.
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # The OIDC claims that identified the run (no token is stored).
    claims: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    artifacts_state: Mapped[str] = mapped_column(Text, nullable=False)
    artifacts_detail: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("build_id", name="uq_run_reports_build_id"),
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_run_reports_digest_format"),
        CheckConstraint(
            "artifacts_state IN ('none', 'pending', 'stored', 'rejected', 'unavailable')",
            name="ck_run_reports_artifacts_state",
        ),
    )


class LinkedReport(Base):
    """A run report that adds to a build instead of describing its own run:
    the hardware-in-the-loop workflow's (services.linked_reports), one per
    build and profile. The run it came from is gh_run_id/gh_run_attempt; a
    later attempt of the same run replaces it."""

    __tablename__ = "linked_reports"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    build_id: Mapped[str] = mapped_column(
        ForeignKey("builds.id", ondelete="CASCADE"), nullable=False
    )
    profile: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. siwx917-hil
    stage: Mapped[str] = mapped_column(Text, nullable=False)  # the build stage it set, e.g. hil
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    claims: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    gh_run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gh_run_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("build_id", "profile", name="uq_linked_reports_build_id_profile"),
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_linked_reports_digest_format"),
    )


class BuildSize(Base):
    """The ELF size a build's run report stated (report "size", from
    ci/size.py), one per build. It is the runner's measurement, shown and
    compared but never gated on: text_bytes_max reads the stored ELF itself."""

    __tablename__ = "build_sizes"

    build_id: Mapped[str] = mapped_column(
        ForeignKey("builds.id", ondelete="CASCADE"), primary_key=True
    )
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    text_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    data_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bss_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # [{"name", "size", "addr"}] as `size -A` lists them.
    sections: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint(
            "text_bytes >= 0 AND data_bytes >= 0 AND bss_bytes >= 0",
            name="ck_build_sizes_nonnegative",
        ),
    )


class GateResult(Base):
    __tablename__ = "gate_results"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    build_id: Mapped[str] = mapped_column(
        ForeignKey("builds.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed: Mapped[Any | None] = mapped_column(JSONB)
    threshold: Mapped[Any | None] = mapped_column(JSONB)
    message: Mapped[str | None] = mapped_column(Text)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("build_id", "rule_id", name="uq_gate_results_build_id_rule_id"),
    )


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    delivery_id: Mapped[str] = mapped_column(Text, primary_key=True)  # X-GitHub-Delivery
    event: Mapped[str] = mapped_column(Text, nullable=False)  # X-GitHub-Event
    action: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        Index("ix_audit_log_target", "target_type", "target_id"),
        Index("ix_audit_log_at", "at"),
    )


class Job(Base):
    __tablename__ = "job_queue"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[JobStatus] = mapped_column(
        pg_enum(JobStatus, "job_status"), nullable=False, server_default=JobStatus.queued.value
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_job_queue_attempts_nonnegative"),
        # Workers poll for ready jobs; only queued rows are indexed.
        Index(
            "ix_job_queue_ready",
            "run_after",
            postgresql_where=text("status = 'queued'"),
        ),
    )
