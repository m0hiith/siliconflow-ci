"""stage log index

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("build_stages", sa.Column("log_index", postgresql.JSONB()))


def downgrade() -> None:
    op.drop_column("build_stages", "log_index")
