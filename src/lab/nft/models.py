"""Таблицы NFT-ветки: коллекции, создатели, лента минтов, allowlist, попытки минта.

История флора и объёма живёт в Parquet (`lab.nft.store`); в Postgres — оперативное
состояние: что отслеживаем, кому какой балл, какие места получены, чем кончились попытки.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from lab.db import Base


class NftCollectionRow(Base):
    __tablename__ = "nft_collections"
    __table_args__ = (UniqueConstraint("chain", "collection", name="uq_nft_collections"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection: Mapped[str] = mapped_column(String(128), nullable=False)
    chain: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    market: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    creator: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    floor: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    volume_24h: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    listed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    holders: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supply: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USD")
    illiquid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    meta_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class NftCreatorRow(Base):
    __tablename__ = "nft_creators"

    creator: Mapped[str] = mapped_column(String(128), primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    collections: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    history_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NftMintUpcomingRow(Base):
    __tablename__ = "nft_mints_upcoming"
    __table_args__ = (UniqueConstraint("chain", "collection", name="uq_nft_mints_upcoming"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection: Mapped[str] = mapped_column(String(128), nullable=False)
    chain: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    market: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    creator: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    supply: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attention: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    creator_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    components_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    sources: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NftAllowlistRow(Base):
    __tablename__ = "nft_allowlist"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    collection: Mapped[str] = mapped_column(String(128), nullable=False)
    chain: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    market: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="raffle")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NftMintAttemptRow(Base):
    """Попытка минта — в том числе неудачная: без неё `mint_cost_failed` не посчитать."""

    __tablename__ = "nft_mint_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    variant: Mapped[str] = mapped_column(String(48), nullable=False, default="")
    collection: Mapped[str] = mapped_column(String(128), nullable=False)
    chain: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    market: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="paper")
    wallet: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    minted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tx: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    reason: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    gas_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=0)
    priority_fee_usd: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=0)
    decision_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirm_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NftPositionRow(Base):
    """Позиция в предмете: сколько продано по лестнице, сколько на удержании, неликвид."""

    __tablename__ = "nft_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    collection: Mapped[str] = mapped_column(String(128), nullable=False)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    chain: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    market: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    qty: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=0)
    sold_qty: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=0)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=0)
    listed_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    illiquid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    illiquid_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
