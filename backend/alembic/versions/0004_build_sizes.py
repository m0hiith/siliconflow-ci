"""build sizes

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "build_sizes",
        sa.Column("build_id", sa.String(32), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("text_bytes", sa.BigInteger(), nullable=False),
        sa.Column("data_bytes", sa.BigInteger(), nullable=False),
        sa.Column("bss_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "sections", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("build_id", name="pk_build_sizes"),
        sa.ForeignKeyConstraint(
            ["build_id"], ["builds.id"],
            name="fk_build_sizes_build_id_builds", ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "text_bytes >= 0 AND data_bytes >= 0 AND bss_bytes >= 0",
            name="ck_build_sizes_nonnegative",
        ),
    )


def downgrade() -> None:
    op.drop_table("build_sizes")
