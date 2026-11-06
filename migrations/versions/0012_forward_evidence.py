"""Forward observation identity and actual journal recording times.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forward_observations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("strategy_id", sa.String(128),
                  sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("signals", sa.Column("recorded_at", sa.DateTime(timezone=True)))
    op.add_column("signals", sa.Column("observation_id", sa.String(32)))
    op.create_foreign_key("fk_signals_observation_id_forward_observations", "signals",
                          "forward_observations", ["observation_id"], ["id"])
    op.add_column("fills", sa.Column("recorded_at", sa.DateTime(timezone=True)))
    # Legacy rows intentionally stay NULL: migration is not proof of timely recording.


def downgrade() -> None:
    op.drop_column("fills", "recorded_at")
    op.drop_constraint("fk_signals_observation_id_forward_observations", "signals",
                       type_="foreignkey")
    op.drop_column("signals", "observation_id")
    op.drop_column("signals", "recorded_at")
    op.drop_table("forward_observations")
