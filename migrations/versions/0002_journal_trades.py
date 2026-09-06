"""journal: qty/instrument/venue/mode/side у сделок, издержки и референс у филлов

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-05 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('trades', sa.Column('instrument', sa.String(length=64), nullable=False, server_default=''))
    op.add_column('trades', sa.Column('venue', sa.String(length=64), nullable=False, server_default=''))
    op.add_column('trades', sa.Column('mode', sa.String(length=32), nullable=False, server_default='paper'))
    op.add_column('trades', sa.Column('side', sa.String(length=32), nullable=False, server_default='long'))
    op.add_column('trades', sa.Column('qty', sa.Numeric(precision=30, scale=12), nullable=False, server_default='0'))
    op.add_column('trades', sa.Column('entry_price', sa.Numeric(precision=30, scale=12), nullable=False, server_default='0'))
    op.add_column('trades', sa.Column('exit_price', sa.Numeric(precision=30, scale=12), nullable=True))
    op.create_index('ix_trades_strategy_closed', 'trades', ['strategy_id', 'closed_at'])
    op.add_column('fills', sa.Column('ref_price', sa.Numeric(precision=30, scale=12), nullable=True))
    op.add_column('fills', sa.Column('costs_json', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='{}'))


def downgrade() -> None:
    op.drop_column('fills', 'costs_json')
    op.drop_column('fills', 'ref_price')
    op.drop_index('ix_trades_strategy_closed', table_name='trades')
    for col in ('exit_price', 'entry_price', 'qty', 'side', 'mode', 'venue', 'instrument'):
        op.drop_column('trades', col)
