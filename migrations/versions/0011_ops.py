"""ops: счётчики квот источников и heartbeat сервисов (тикет 14, R25.1, R32i.2)

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-07 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'feed_usage',
        sa.Column('feed_id', sa.String(length=64), primary_key=True),
        sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('calls', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('spent_usd', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('month', sa.String(length=7), nullable=False, server_default=''),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_table(
        'service_heartbeats',
        sa.Column('service', sa.String(length=32), primary_key=True),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('detail', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('alerted_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('service_heartbeats')
    op.drop_table('feed_usage')
