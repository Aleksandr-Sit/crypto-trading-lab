"""outbox: повтор с задержкой, ссылка на сообщение Telegram, причина последней неудачи

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-06 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('outbox', sa.Column('kind', sa.String(length=32), nullable=False, server_default='alert'))
    op.add_column('outbox', sa.Column('ref', sa.String(length=128), nullable=True))
    op.add_column('outbox', sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('outbox', sa.Column('last_error', sa.Text(), nullable=True))
    op.add_column('outbox', sa.Column('message_id', sa.Integer(), nullable=True))
    op.add_column('outbox', sa.Column('dead', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index('ix_outbox_ref', 'outbox', ['ref'])
    op.create_index('ix_outbox_pending', 'outbox', ['delivered_at', 'dead', 'next_attempt_at'])


def downgrade() -> None:
    op.drop_index('ix_outbox_pending', table_name='outbox')
    op.drop_index('ix_outbox_ref', table_name='outbox')
    for col in ('dead', 'message_id', 'last_error', 'next_attempt_at', 'ref', 'kind'):
        op.drop_column('outbox', col)
