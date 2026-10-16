"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-14
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = {
    "build_status": ("queued", "running", "passed", "failed", "cancelled", "timed_out", "errored"),
    "stage_status": ("pending", "running", "passed", "failed", "skipped", "cancelled"),
    "build_trigger": ("push", "pull_request", "manual"),
    "test_outcome": ("passed", "failed", "skipped", "error"),
    "job_status": ("queued", "running", "done", "failed"),
    "user_role": ("viewer", "maintainer"),
}


def enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def ts(name: str, nullable: bool = True, default_now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if default_now else None,
    )


def finished_after_started(table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
        name=f"ck_{table}_finished_after_started",
    )


ID = sa.String(32)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind)

    op.create_table(
        "users",
        sa.Column("id", ID, nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("avatar_url", sa.Text()),
        sa.Column("role", enum("user_role"), nullable=False, server_default="viewer"),
        ts("created_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("github_id", name="uq_users_github_id"),
        sa.UniqueConstraint("login", name="uq_users_login"),
    )

    op.create_table(
        "projects",
        sa.Column("id", ID, nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=False, server_default="main"),
        sa.Column("next_build_number", sa.Integer(), nullable=False, server_default="1"),
        ts("created_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        sa.UniqueConstraint("slug", name="uq_projects_slug"),
        sa.CheckConstraint("next_build_number >= 1", name="ck_projects_next_build_number_positive"),
    )

    op.create_table(
        "repositories",
        sa.Column("id", ID, nullable=False),
        sa.Column("project_id", ID, nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("gh_repo_id", sa.BigInteger()),
        sa.Column("gh_installation_id", sa.BigInteger()),
        sa.Column("default_branch", sa.Text(), nullable=False, server_default="main"),
        ts("created_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_repositories"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            name="fk_repositories_project_id_projects", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("full_name", name="uq_repositories_full_name"),
        sa.UniqueConstraint("gh_repo_id", name="uq_repositories_gh_repo_id"),
    )
    op.create_index("ix_repositories_project_id", "repositories", ["project_id"])

    op.create_table(
        "builds",
        sa.Column("id", ID, nullable=False),
        sa.Column("project_id", ID, nullable=False),
        sa.Column("repository_id", ID, nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", enum("build_status"), nullable=False, server_default="queued"),
        sa.Column("trigger", enum("build_trigger"), nullable=False),
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text()),
        sa.Column("commit_sha", sa.String(40)),
        sa.Column("commit_message", sa.Text()),
        sa.Column("author_login", sa.Text()),
        sa.Column("pr_number", sa.Integer()),
        sa.Column("gh_run_id", sa.BigInteger()),
        sa.Column("gh_run_attempt", sa.Integer()),
        sa.Column("gh_conclusion", sa.Text()),
        sa.Column("status_reason", sa.Text()),
        sa.Column("failure_class", sa.Text()),
        sa.Column("triggered_by_user_id", ID),
        ts("queued_at", nullable=False, default_now=True),
        ts("started_at"),
        ts("finished_at"),
        ts("created_at", nullable=False, default_now=True),
        ts("updated_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_builds"),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            name="fk_builds_project_id_projects", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["repositories.id"],
            name="fk_builds_repository_id_repositories", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["triggered_by_user_id"], ["users.id"],
            name="fk_builds_triggered_by_user_id_users", ondelete="SET NULL",
        ),
        sa.UniqueConstraint("project_id", "number", name="uq_builds_project_id_number"),
        sa.UniqueConstraint(
            "gh_run_id", "gh_run_attempt", name="uq_builds_gh_run_id_gh_run_attempt"
        ),
        finished_after_started("builds"),
        sa.CheckConstraint("number >= 1", name="ck_builds_number_positive"),
        sa.CheckConstraint(
            "(gh_run_id IS NULL) = (gh_run_attempt IS NULL)", name="ck_builds_gh_run_pair"
        ),
        sa.CheckConstraint(
            "gh_run_attempt IS NULL OR gh_run_attempt >= 1",
            name="ck_builds_gh_run_attempt_positive",
        ),
        sa.CheckConstraint(
            "commit_sha IS NULL OR commit_sha ~ '^[0-9a-f]{40}$'",
            name="ck_builds_commit_sha_format",
        ),
    )
    op.create_index("ix_builds_created_at_id", "builds", ["created_at", "id"])
    op.create_index("ix_builds_project_id_created_at", "builds", ["project_id", "created_at"])
    op.create_index("ix_builds_status", "builds", ["status"])
    op.create_index("ix_builds_branch", "builds", ["branch"])
    op.create_index("ix_builds_commit_sha", "builds", ["commit_sha"])

    op.create_table(
        "build_stages",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("build_id", ID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("status", enum("stage_status"), nullable=False, server_default="pending"),
        sa.Column("exit_code", sa.Integer()),
        ts("started_at"),
        ts("finished_at"),
        sa.Column("log_key", sa.Text()),
        sa.PrimaryKeyConstraint("id", name="pk_build_stages"),
        sa.ForeignKeyConstraint(
            ["build_id"], ["builds.id"],
            name="fk_build_stages_build_id_builds", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("build_id", "name", name="uq_build_stages_build_id_name"),
        finished_after_started("build_stages"),
    )

    op.create_table(
        "test_cases",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("build_id", ID, nullable=False),
        sa.Column("suite", sa.Text(), nullable=False, server_default=""),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("classname", sa.Text()),
        sa.Column("outcome", enum("test_outcome"), nullable=False),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("message", sa.Text()),
        sa.PrimaryKeyConstraint("id", name="pk_test_cases"),
        sa.ForeignKeyConstraint(
            ["build_id"], ["builds.id"],
            name="fk_test_cases_build_id_builds", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("build_id", "suite", "name", name="uq_test_cases_build_id_suite_name"),
    )
    op.create_index("ix_test_cases_build_id_outcome", "test_cases", ["build_id", "outcome"])

    op.create_table(
        "artifacts",
        sa.Column("id", ID, nullable=False),
        sa.Column("build_id", ID, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.Text()),
        sa.Column(
            "release_eligible", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        ts("created_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_artifacts"),
        sa.ForeignKeyConstraint(
            ["build_id"], ["builds.id"],
            name="fk_artifacts_build_id_builds", ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "build_id", "kind", "sha256", name="uq_artifacts_build_id_kind_sha256"
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_artifacts_size_nonnegative"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_artifacts_sha256_format"),
    )
    op.create_index("ix_artifacts_sha256", "artifacts", ["sha256"])

    op.create_table(
        "gate_results",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("build_id", ID, nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("observed", postgresql.JSONB()),
        sa.Column("threshold", postgresql.JSONB()),
        sa.Column("message", sa.Text()),
        ts("evaluated_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_gate_results"),
        sa.ForeignKeyConstraint(
            ["build_id"], ["builds.id"],
            name="fk_gate_results_build_id_builds", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("build_id", "rule_id", name="uq_gate_results_build_id_rule_id"),
    )

    op.create_table(
        "webhook_events",
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("action", sa.Text()),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        ts("received_at", nullable=False, default_now=True),
        ts("processed_at"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text()),
        sa.PrimaryKeyConstraint("delivery_id", name="pk_webhook_events"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        ts("at", nullable=False, default_now=True),
        sa.Column("actor_user_id", ID),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"],
            name="fk_audit_log_actor_user_id_users", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_audit_log_target", "audit_log", ["target_type", "target_id"])
    op.create_index("ix_audit_log_at", "audit_log", ["at"])

    op.create_table(
        "job_queue",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("status", enum("job_status"), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        ts("run_after", nullable=False, default_now=True),
        ts("locked_at"),
        sa.Column("locked_by", sa.Text()),
        sa.Column("last_error", sa.Text()),
        ts("created_at", nullable=False, default_now=True),
        ts("updated_at", nullable=False, default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_job_queue"),
        sa.CheckConstraint("attempts >= 0", name="ck_job_queue_attempts_nonnegative"),
    )
    op.create_index(
        "ix_job_queue_ready",
        "job_queue",
        ["run_after"],
        postgresql_where=sa.text("status = 'queued'"),
    )


def downgrade() -> None:
    for table in (
        "job_queue",
        "audit_log",
        "webhook_events",
        "gate_results",
        "artifacts",
        "test_cases",
        "build_stages",
        "builds",
        "repositories",
        "projects",
        "users",
    ):
        op.drop_table(table)  # drops the table's indexes and constraints too
    bind = op.get_bind()
    for name in reversed(ENUMS):
        postgresql.ENUM(name=name).drop(bind)
