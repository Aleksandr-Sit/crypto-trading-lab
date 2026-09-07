"""Таблица `copy_trades` (миграция 0006): сделка лидера и её судьба у нас.

Ссылка на сделку лидера и лаг в миллисекундах живут в строке, а не в логах: без них
отчёт «копия vs лидер» (`copy_lag_cost`, доля пропущенных) собрать не из чего.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from lab.db.base import Base, utcnow


class CopyTradeRow(Base):
    __tablename__ = "copy_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(128), index=True)
    chain: Mapped[str] = mapped_column(String(32))
    leader_address: Mapped[str] = mapped_column(String(128), index=True)
    leader_tx: Mapped[str] = mapped_column(String(128))
    leader_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    leader_side: Mapped[str] = mapped_column(String(8))
    leader_qty: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    leader_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    signal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    copy_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    lag_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="copied")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


__all__ = ["CopyTradeRow"]
