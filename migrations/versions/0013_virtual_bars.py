"""Immutable bars consumed by the virtual collector, with archive reconciliation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "virtual_bars",
        sa.Column("observation_id", sa.String(32),
                  sa.ForeignKey("forward_observations.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("instrument", sa.String(64), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archive_status", sa.String(32), nullable=False),
    )


def downgrade():
    op.drop_table("virtual_bars")
