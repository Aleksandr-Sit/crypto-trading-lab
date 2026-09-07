"""Таблица предложений перелива (G05, миграция 0010).

Деньги двигает пользователь руками — система только предлагает сумму и записывает
факт по подтверждению (G05.1): `proposed` → `moved` | `skipped`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from lab.db import Base


class RebalanceProposalRow(Base):
    __tablename__ = "rebalance_proposals"
    __table_args__ = (Index("ix_rebalance_proposals_branch", "from_branch", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_branch: Mapped[str] = mapped_column(String(32), nullable=False)
    to_branch: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    base_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=0)
    current_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    detail: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str] = mapped_column(String(32), nullable=False, default="")


__all__ = ["RebalanceProposalRow"]
