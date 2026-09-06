"""Таблицы ядра из спецификации («Схема данных»). Имена классов — *Row, чтобы не путать
с pydantic-типами контрактов."""

from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lab.db.base import Base, Code, Json, JsonList, Money, MoneyOpt, Ts, TsOpt, utcnow


class StrategyRow(Base):
    __tablename__ = "strategies"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64))
    branch: Mapped[Code]
    venue: Mapped[str] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str | None] = mapped_column(String(256))
    instruments: Mapped[JsonList]
    timeframe: Mapped[str | None] = mapped_column(String(16))
    rung: Mapped[Code]
    status: Mapped[Code]
    params_json: Mapped[Json]
    can_backtest: Mapped[bool] = mapped_column(Boolean, default=True)
    stop_json: Mapped[Json]
    valid_until: Mapped[TsOpt]
    description: Mapped[str] = mapped_column(Text, default="")
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[Ts] = mapped_column(default=utcnow)
    updated_at: Mapped[Ts] = mapped_column(default=utcnow, onupdate=utcnow)
    retired_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_strategies_branch_status", "branch", "status"),)


class CandidateRow(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))
    ref: Mapped[str] = mapped_column(String(256))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[Json] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[Ts] = mapped_column(default=utcnow)
    decision: Mapped[Code] = mapped_column(default="pending")
    decided_at: Mapped[TsOpt]


class MeasurementRow(Base):
    __tablename__ = "measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    mode: Mapped[Code]
    window_from: Mapped[Ts]
    window_to: Mapped[Ts]
    data_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(64))
    metrics_json: Mapped[Json]
    threshold_json: Mapped[Json]
    status: Mapped[Code]
    created_at: Mapped[Ts] = mapped_column(default=utcnow)


class SignalRow(Base):
    __tablename__ = "signals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    decided_at: Mapped[Ts]
    instrument: Mapped[str] = mapped_column(String(64))
    side: Mapped[Code]
    size: Mapped[Money]
    price_ref: Mapped[MoneyOpt]
    inputs_hash: Mapped[str] = mapped_column(String(64))
    ttl: Mapped[int] = mapped_column(Integer)
    meta: Mapped[Json] = mapped_column(default=dict)
    outcome: Mapped[Code] = mapped_column(default="pending")
    outcome_at: Mapped[TsOpt]

    __table_args__ = (Index("ix_signals_strategy_decided", "strategy_id", "decided_at"),)


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signal_id: Mapped[str] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"))
    venue: Mapped[str] = mapped_column(String(64))
    client_order_id: Mapped[str] = mapped_column(String(128))
    mode: Mapped[Code]
    state: Mapped[Code]
    instrument: Mapped[str] = mapped_column(String(64))
    side: Mapped[Code]
    order_type: Mapped[Code]
    qty: Mapped[Money]
    price: Mapped[MoneyOpt]
    filled_qty: Mapped[Money] = mapped_column(default=Decimal(0))
    leverage: Mapped[Money] = mapped_column(default=Decimal(1))
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_verdict: Mapped[Json] = mapped_column(default=dict)
    created_at: Mapped[Ts] = mapped_column(default=utcnow)
    updated_at: Mapped[Ts] = mapped_column(default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("venue", "client_order_id"),)


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    price: Mapped[Money]
    qty: Mapped[Money]
    fee: Mapped[Money]
    fee_asset: Mapped[str] = mapped_column(String(16))
    ts: Mapped[Ts]
    # таск 02 (миграция 0002): референсная цена на момент решения и издержки по компонентам
    ref_price: Mapped[MoneyOpt]
    costs_json: Mapped[Json] = mapped_column(default=dict)


class TradeRow(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    open_fill: Mapped[str] = mapped_column(ForeignKey("fills.id"))
    close_fill: Mapped[str | None] = mapped_column(ForeignKey("fills.id"))
    pnl_gross: Mapped[Money]
    fee: Mapped[Money]
    slippage: Mapped[Money]
    funding: Mapped[Money]
    gas: Mapped[Money]
    priority_fee: Mapped[Money] = mapped_column(default=Decimal(0))
    royalty: Mapped[Money] = mapped_column(default=Decimal(0))
    pnl_net: Mapped[Money]
    opened_at: Mapped[Ts]
    closed_at: Mapped[TsOpt]
    # таск 02 (миграция 0002): FIFO-связывание частичных объёмов
    instrument: Mapped[str] = mapped_column(String(64), default="")
    venue: Mapped[str] = mapped_column(String(64), default="")
    mode: Mapped[Code] = mapped_column(default="paper")
    side: Mapped[Code] = mapped_column(default="long")
    qty: Mapped[Money] = mapped_column(default=Decimal(0))
    entry_price: Mapped[Money] = mapped_column(default=Decimal(0))
    exit_price: Mapped[MoneyOpt]

    __table_args__ = (Index("ix_trades_strategy_closed", "strategy_id", "closed_at"),)


class RungTransitionRow(Base):
    __tablename__ = "rung_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id", ondelete="CASCADE"))
    from_rung: Mapped[Code] = mapped_column("from")
    to_rung: Mapped[Code] = mapped_column("to")
    reason: Mapped[str] = mapped_column(Text)
    metrics_snapshot: Mapped[Json]
    ts: Mapped[Ts] = mapped_column(default=utcnow)
    by: Mapped[Code]


class FeedRow(Base):
    __tablename__ = "feeds"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    quota_limit: Mapped[int | None] = mapped_column(Integer)
    quota_period: Mapped[str | None] = mapped_column(String(16))
    quota_used: Mapped[int] = mapped_column(Integer, default=0)
    health: Mapped[Code] = mapped_column(default="ok")
    health_detail: Mapped[str] = mapped_column(Text, default="")
    checked_at: Mapped[TsOpt]
    cost_month: Mapped[Money] = mapped_column(default=Decimal(0))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    fallback_id: Mapped[str | None] = mapped_column(ForeignKey("feeds.id"))


class WalletTrackedRow(Base):
    __tablename__ = "wallets_tracked"

    address: Mapped[str] = mapped_column(String(128), primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), primary_key=True)
    stats_json: Mapped[Json] = mapped_column(default=dict)
    flags_json: Mapped[JsonList] = mapped_column(default=list)
    last_recalc: Mapped[TsOpt]


class CollectionTrackedRow(Base):
    __tablename__ = "collections_tracked"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(32))
    slug: Mapped[str] = mapped_column(String(128))
    creator: Mapped[str | None] = mapped_column(String(128))
    stats_json: Mapped[Json] = mapped_column(default=dict)
    tracked_since: Mapped[Ts] = mapped_column(default=utcnow)
    last_update: Mapped[TsOpt]

    __table_args__ = (UniqueConstraint("chain", "market", "slug"),)


class CreatorRow(Base):
    __tablename__ = "creators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(32))
    address: Mapped[str] = mapped_column(String(128))
    name: Mapped[str | None] = mapped_column(String(128))
    score: Mapped[MoneyOpt]
    stats_json: Mapped[Json] = mapped_column(default=dict)
    updated_at: Mapped[TsOpt]

    __table_args__ = (UniqueConstraint("chain", "address"),)


class MintUpcomingRow(Base):
    __tablename__ = "mints_upcoming"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chain: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(32))
    collection: Mapped[str] = mapped_column(String(128))
    creator: Mapped[str | None] = mapped_column(String(128))
    starts_at: Mapped[TsOpt]
    price: Mapped[MoneyOpt]
    supply: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[Json] = mapped_column(default=dict)
    discovered_at: Mapped[Ts] = mapped_column(default=utcnow)

    __table_args__ = (UniqueConstraint("chain", "market", "collection"),)


class SignalPublicRow(Base):
    """Журнал авторов: публичные сигналы каналов (форвард-журнал, R07)."""

    __tablename__ = "signals_public"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    author: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(128))
    message_ref: Mapped[str] = mapped_column(String(128))
    published_at: Mapped[Ts]
    captured_at: Mapped[Ts] = mapped_column(default=utcnow)
    instrument: Mapped[str | None] = mapped_column(String(64))
    side: Mapped[str | None] = mapped_column(String(16))
    price_ref: Mapped[MoneyOpt]
    raw_text: Mapped[str] = mapped_column(Text)
    parsed_json: Mapped[Json] = mapped_column(default=dict)
    outcome_json: Mapped[Json] = mapped_column(default=dict)

    __table_args__ = (UniqueConstraint("channel", "message_ref"),)


class AllocationRow(Base):
    __tablename__ = "allocations"

    branch: Mapped[str] = mapped_column(String(32), primary_key=True)
    share: Mapped[Money]
    base_amount: Mapped[Money]
    current_amount: Mapped[Money]
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[Ts] = mapped_column(default=utcnow, onupdate=utcnow)


class OutboxRow(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[Json]
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[Ts] = mapped_column(default=utcnow)
    delivered_at: Mapped[TsOpt]


class ConfigChangeRow(Base):
    """Журнал изменений конфига (R30i.4, миграция 0003): кто, когда, какой файл, diff."""

    __tablename__ = "config_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    who: Mapped[str] = mapped_column(String(64))
    ts: Mapped[Ts] = mapped_column(default=utcnow)
    path: Mapped[str] = mapped_column(String(256))
    diff: Mapped[Json] = mapped_column(default=dict)


class SystemFlagRow(Base):
    """Флаги системы, общие для процессов (миграция 0003): например «стоп всё» (G04.1)."""

    __tablename__ = "system_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Json] = mapped_column(default=dict)
    updated_by: Mapped[str] = mapped_column(String(64), default="system")
    updated_at: Mapped[Ts] = mapped_column(default=utcnow, onupdate=utcnow)


ALL_TABLES: tuple[str, ...] = tuple(Base.metadata.tables)
