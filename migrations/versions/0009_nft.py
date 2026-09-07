"""nft: коллекции, создатели, лента минтов, allowlist, попытки минта, позиции (тикет 10)

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-07 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'nft_collections',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('collection', sa.String(length=128), nullable=False),
        sa.Column('chain', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('market', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('name', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('creator', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('floor', sa.Numeric(38, 18), nullable=True),
        sa.Column('volume_24h', sa.Numeric(38, 18), nullable=True),
        sa.Column('listed', sa.Integer(), nullable=True),
        sa.Column('holders', sa.Integer(), nullable=True),
        sa.Column('supply', sa.Integer(), nullable=True),
        sa.Column('currency', sa.String(length=16), nullable=False, server_default='USD'),
        sa.Column('illiquid', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('meta_json', sa.JSON(), nullable=False, server_default='{}'),
        sa.UniqueConstraint('chain', 'collection', name='uq_nft_collections'),
    )
    op.create_index('ix_nft_collections_creator', 'nft_collections', ['creator'])

    op.create_table(
        'nft_creators',
        sa.Column('creator', sa.String(length=128), primary_key=True),
        sa.Column('chain', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('collections', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('successful', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('success_rate_pct', sa.Numeric(12, 4), nullable=True),
        sa.Column('score', sa.Numeric(12, 6), nullable=True),
        sa.Column('history_json', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('computed_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )

    op.create_table(
        'nft_mints_upcoming',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('collection', sa.String(length=128), nullable=False),
        sa.Column('chain', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('market', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('creator', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('starts_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('price', sa.Numeric(38, 18), nullable=True),
        sa.Column('supply', sa.Integer(), nullable=True),
        sa.Column('attention', sa.Numeric(12, 6), nullable=True),
        sa.Column('creator_score', sa.Numeric(12, 6), nullable=True),
        sa.Column('components_json', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('sources', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('seen_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint('chain', 'collection', name='uq_nft_mints_upcoming'),
    )
    op.create_index('ix_nft_mints_upcoming_starts_at', 'nft_mints_upcoming', ['starts_at'])

    op.create_table(
        'nft_allowlist',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('collection', sa.String(length=128), nullable=False),
        sa.Column('chain', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('market', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('kind', sa.String(length=32), nullable=False, server_default='raffle'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='open'),
        sa.Column('url', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('note', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('deadline', sa.DateTime(timezone=True), nullable=True),
        sa.Column('mint_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('seat_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_nft_allowlist_status', 'nft_allowlist', ['status'])

    op.create_table(
        'nft_mint_attempts',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('strategy_id', sa.String(length=120), nullable=False, server_default=''),
        sa.Column('variant', sa.String(length=48), nullable=False, server_default=''),
        sa.Column('collection', sa.String(length=128), nullable=False),
        sa.Column('chain', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('market', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('mode', sa.String(length=8), nullable=False, server_default='paper'),
        sa.Column('wallet', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('attempt', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('ok', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('minted', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tx', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('reason', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('price', sa.Numeric(38, 18), nullable=True),
        sa.Column('gas_usd', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('priority_fee_usd', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('decision_latency_ms', sa.Integer(), nullable=True),
        sa.Column('confirm_latency_ms', sa.Integer(), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index('ix_nft_mint_attempts_strategy', 'nft_mint_attempts', ['strategy_id'])

    op.create_table(
        'nft_positions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('strategy_id', sa.String(length=120), nullable=False, server_default=''),
        sa.Column('collection', sa.String(length=128), nullable=False),
        sa.Column('token_id', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('chain', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('market', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('qty', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('sold_qty', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('entry_price', sa.Numeric(38, 18), nullable=False, server_default='0'),
        sa.Column('listed_price', sa.Numeric(38, 18), nullable=True),
        sa.Column('listed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('illiquid', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('illiquid_json', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_nft_positions_strategy', 'nft_positions', ['strategy_id'])


def downgrade() -> None:
    op.drop_index('ix_nft_positions_strategy', table_name='nft_positions')
    op.drop_table('nft_positions')
    op.drop_index('ix_nft_mint_attempts_strategy', table_name='nft_mint_attempts')
    op.drop_table('nft_mint_attempts')
    op.drop_index('ix_nft_allowlist_status', table_name='nft_allowlist')
    op.drop_table('nft_allowlist')
    op.drop_index('ix_nft_mints_upcoming_starts_at', table_name='nft_mints_upcoming')
    op.drop_table('nft_mints_upcoming')
    op.drop_table('nft_creators')
    op.drop_index('ix_nft_collections_creator', table_name='nft_collections')
    op.drop_table('nft_collections')
