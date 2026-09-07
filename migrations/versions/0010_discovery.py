"""discovery/ops: предложения перелива излишка рисковых веток (тикет 12, G05)

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-07 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'rebalance_proposals',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('from_branch', sa.String(length=32), nullable=False),
        sa.Column('to_branch', sa.String(length=32), nullable=False),
        sa.Column('amount_usd', sa.Numeric(38, 18), nullable=False),
        sa.Column('base_usd', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('current_usd', sa.Numeric(38, 18), nullable=False, server_default='0'),
        # proposed → moved (пользователь перевёл руками и подтвердил) | skipped
        sa.Column('status', sa.String(length=16), nullable=False, server_default='proposed'),
        sa.Column('detail', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decided_by', sa.String(length=32), nullable=False, server_default=''),
    )
    op.create_index(
        'ix_rebalance_proposals_branch', 'rebalance_proposals', ['from_branch', 'created_at']
    )


def downgrade() -> None:
    op.drop_index('ix_rebalance_proposals_branch', table_name='rebalance_proposals')
    op.drop_table('rebalance_proposals')
