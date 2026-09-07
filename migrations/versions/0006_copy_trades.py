"""copy_trades: сделка лидера, её копия, лаг и причина пропуска (тикет 08)

Revision ID: 0006
Revises: 0004
Create Date: 2026-09-07 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'copy_trades',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('strategy_id', sa.String(length=128), nullable=False),
        sa.Column('chain', sa.String(length=32), nullable=False),
        sa.Column('leader_address', sa.String(length=128), nullable=False),
        sa.Column('leader_tx', sa.String(length=128), nullable=False),
        sa.Column('leader_ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('leader_side', sa.String(length=8), nullable=False),
        sa.Column('leader_qty', sa.Numeric(38, 18), nullable=False),
        sa.Column('leader_price', sa.Numeric(38, 18), nullable=False),
        sa.Column('signal_id', sa.String(length=64), nullable=True),
        sa.Column('order_id', sa.String(length=64), nullable=True),
        sa.Column('copy_price', sa.Numeric(38, 18), nullable=True),
        sa.Column('lag_ms', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='copied'),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_copy_trades_strategy_id', 'copy_trades', ['strategy_id'])
    op.create_index('ix_copy_trades_leader_address', 'copy_trades', ['leader_address'])
    op.create_index('ix_copy_trades_leader', 'copy_trades', ['leader_address', 'leader_ts'])


def downgrade() -> None:
    op.drop_index('ix_copy_trades_leader', table_name='copy_trades')
    op.drop_index('ix_copy_trades_leader_address', table_name='copy_trades')
    op.drop_index('ix_copy_trades_strategy_id', table_name='copy_trades')
    op.drop_table('copy_trades')
