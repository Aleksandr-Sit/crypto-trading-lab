"""risk/ladder: журнал изменений конфига и флаги системы («стоп всё»)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-05 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'config_changes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('who', sa.String(length=64), nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('path', sa.String(length=256), nullable=False),
        sa.Column('diff', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_config_changes')),
    )
    op.create_table(
        'system_flags',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('updated_by', sa.String(length=64), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key', name=op.f('pk_system_flags')),
    )


def downgrade() -> None:
    op.drop_table('system_flags')
    op.drop_table('config_changes')
