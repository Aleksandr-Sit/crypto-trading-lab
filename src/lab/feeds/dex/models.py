"""Строки ветки `meme` (миграция 0008). Схема ядра — таск 01, эти таблицы — свои."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from lab.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MemeTokenRow(Base):
    """Токен потока. Полная история — только у прошедших фильтр (`passed_filter`)."""

    __tablename__ = "meme_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(32))
    address: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(128), default="")
    source: Mapped[str] = mapped_column(String(32), default="")
    venue: Mapped[str] = mapped_column(String(32), default="")
    pair: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    passed_filter: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(32), default="")
    price_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    liquidity_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    volume_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    honesty_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MemeTokenSnapshotRow(Base):
    """Точка истории токена — пишется только по прошедшим первичный фильтр."""

    __tablename__ = "meme_token_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(32))
    address: Mapped[str] = mapped_column(String(128))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    price_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    liquidity_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    volume_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    buys: Mapped[int | None] = mapped_column(Integer)
    sells: Mapped[int | None] = mapped_column(Integer)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class MemeFlowStatsRow(Base):
    """Агрегаты по окну потока — то, что остаётся от отсеянных токенов."""

    __tablename__ = "meme_flow_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    window_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seen: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasons_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    chains_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sources_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    @property
    def reasons(self) -> dict[str, int]:
        return dict(self.reasons_json or {})


class MemeTxAttemptRow(Base):
    """Попытка транзакции, включая неудачную: причина и стоимость (История 70)."""

    __tablename__ = "meme_tx_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(128), default="")
    venue: Mapped[str] = mapped_column(String(32), default="")
    order_id: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16))
    tx: Mapped[str] = mapped_column(String(128), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    gas_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18), default=Decimal(0))
    priority_fee_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18), default=Decimal(0))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


__all__ = ["MemeFlowStatsRow", "MemeTokenRow", "MemeTokenSnapshotRow", "MemeTxAttemptRow"]
