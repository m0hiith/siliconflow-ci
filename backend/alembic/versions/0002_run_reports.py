"""run reports

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("build_id", sa.String(32), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("claims", postgresql.JSONB(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("artifacts_state", sa.Text(), nullable=False),
        sa.Column("artifacts_detail", sa.Text()),
        sa.PrimaryKeyConstraint("id", name="pk_run_reports"),
        sa.ForeignKeyConstraint(
            ["build_id"], ["builds.id"],
            name="fk_run_reports_build_id_builds", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("build_id", name="uq_run_reports_build_id"),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_run_reports_digest_format"),
        sa.CheckConstraint(
            "artifacts_state IN ('none', 'pending', 'stored', 'rejected', 'unavailable')",
            name="ck_run_reports_artifacts_state",
        ),
    )


def downgrade() -> None:
    op.drop_table("run_reports")
