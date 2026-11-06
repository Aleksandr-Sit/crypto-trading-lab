"""Durable record of research attempts, including failures and rejected variants."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "research_trials",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("strategy_id", sa.String(128), nullable=False),
        sa.Column("variant_hash", sa.String(64), nullable=False),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_research_trials_strategy_id", "research_trials", ["strategy_id"])
    op.create_index("ix_research_trials_variant_hash", "research_trials", ["variant_hash"])
    # Only persisted historical attempts are recoverable; never invent missing trials.
    op.execute("""
        INSERT INTO research_trials
            (strategy_id, variant_hash, spec, result, status, reason, started_at, finished_at)
        SELECT strategy_id, md5('legacy:' || id::text),
               jsonb_build_object('legacy_measurement_id', id, 'coverage', 'snapshot_only'),
               threshold_json, status, 'imported historical snapshot', created_at, created_at
        FROM measurements
    """)


def downgrade():
    op.drop_table("research_trials")
