"""meme: поток токенов, снимки прошедших фильтр, агрегаты и попытки транзакций (тикет 09)

Revision ID: 0008
Revises: 0006
Create Date: 2026-09-07 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0008'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'meme_tokens',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('chain', sa.String(length=32), nullable=False),
        sa.Column('address', sa.String(length=128), nullable=False),
        sa.Column('symbol', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('name', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('source', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('venue', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('pair', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('passed_filter', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('reason', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('price_usd', sa.Numeric(38, 18), nullable=True),
        sa.Column('liquidity_usd', sa.Numeric(38, 18), nullable=True),
        sa.Column('volume_usd', sa.Numeric(38, 18), nullable=True),
        sa.Column('honesty_json', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('snapshot_json', sa.JSON(), nullable=False, server_default='{}'),
        sa.UniqueConstraint('chain', 'address', name='uq_meme_tokens_chain_address'),
    )
    op.create_index('ix_meme_tokens_passed', 'meme_tokens', ['passed_filter', 'last_seen_at'])

    op.create_table(
        'meme_token_snapshots',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('chain', sa.String(length=32), nullable=False),
        sa.Column('address', sa.String(length=128), nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('price_usd', sa.Numeric(38, 18), nullable=True),
        sa.Column('liquidity_usd', sa.Numeric(38, 18), nullable=True),
        sa.Column('volume_usd', sa.Numeric(38, 18), nullable=True),
        sa.Column('buys', sa.Integer(), nullable=True),
        sa.Column('sells', sa.Integer(), nullable=True),
        sa.Column('payload_json', sa.JSON(), nullable=False, server_default='{}'),
    )
    op.create_index(
        'ix_meme_token_snapshots_token', 'meme_token_snapshots', ['chain', 'address', 'ts']
    )

    op.create_table(
        'meme_flow_stats',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('window_from', sa.DateTime(timezone=True), nullable=False),
        sa.Column('window_to', sa.DateTime(timezone=True), nullable=False),
        sa.Column('seen', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('passed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('rejected', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('reasons_json', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('chains_json', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('sources_json', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_meme_flow_stats_window', 'meme_flow_stats', ['window_from'])

    op.create_table(
        'meme_tx_attempts',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('strategy_id', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('venue', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('order_id', sa.String(length=64), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('tx', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('gas_usd', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('priority_fee_usd', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_meme_tx_attempts_order', 'meme_tx_attempts', ['order_id', 'attempt'])


def downgrade() -> None:
    op.drop_index('ix_meme_tx_attempts_order', table_name='meme_tx_attempts')
    op.drop_table('meme_tx_attempts')
    op.drop_index('ix_meme_flow_stats_window', table_name='meme_flow_stats')
    op.drop_table('meme_flow_stats')
    op.drop_index('ix_meme_token_snapshots_token', table_name='meme_token_snapshots')
    op.drop_table('meme_token_snapshots')
    op.drop_index('ix_meme_tokens_passed', table_name='meme_tokens')
    op.drop_table('meme_tokens')
