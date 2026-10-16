"""linked reports

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "linked_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("build_id", sa.String(32), nullable=False),
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("claims", postgresql.JSONB(), nullable=False),
        sa.Column("gh_run_id", sa.BigInteger(), nullable=False),
        sa.Column("gh_run_attempt", sa.Integer(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_linked_reports"),
        sa.ForeignKeyConstraint(
            ["build_id"], ["builds.id"],
            name="fk_linked_reports_build_id_builds", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("build_id", "profile", name="uq_linked_reports_build_id_profile"),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_linked_reports_digest_format"),
    )


def downgrade() -> None:
    op.drop_table("linked_reports")
