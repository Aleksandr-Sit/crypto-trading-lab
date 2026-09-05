"""Типы контрактов (pydantic v2). Деньги — Decimal, время — aware UTC."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lab.contracts.enums import Branch, OrderState

Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
ModeLiteral = Literal["paper", "live"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return value.astimezone(UTC)


class OrderIntent(_Model):
    strategy_id: str
    venue: str
    instrument: str
    side: Side
    qty: Decimal = Field(gt=0)
    price: Decimal | None = None
    order_type: OrderType
    leverage: Decimal = Decimal(1)
    reduce_only: bool = False
    mode: ModeLiteral
    signal_id: str
    client_order_id: str


class Signal(_Model):
    strategy_id: str
    decided_at: datetime
    instrument: str
    side: str
    size: Decimal
    price_ref: Decimal | None = None
    inputs_hash: str
    ttl_s: int = Field(ge=0)
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("decided_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value)


class Costs(_Model):
    """Издержки (Словарь): комиссия + проскальзывание + фандинг + газ + приоритетная fee + роялти.
    Спред входит в slippage."""

    fee: Decimal = Decimal(0)
    slippage: Decimal = Decimal(0)
    funding: Decimal = Decimal(0)
    gas: Decimal = Decimal(0)
    priority_fee: Decimal = Decimal(0)
    royalty: Decimal = Decimal(0)

    @property
    def total(self) -> Decimal:
        return self.fee + self.slippage + self.funding + self.gas + self.priority_fee + self.royalty


class Health(_Model):
    status: Literal["ok", "degraded", "down"]
    detail: str = ""
    checked_at: datetime

    @field_validator("checked_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value)


class KeyRights(_Model):
    """Права API-ключа. withdraw=True → площадка не используется (решение §13)."""

    trade: bool
    withdraw: bool


class Order(_Model):
    id: str
    signal_id: str
    venue: str
    client_order_id: str
    mode: ModeLiteral
    state: OrderState
    instrument: str
    side: Side
    qty: Decimal
    price: Decimal | None = None
    filled_qty: Decimal = Decimal(0)
    created_at: datetime
    reason: str = ""


class Fill(_Model):
    id: str
    order_id: str
    price: Decimal
    qty: Decimal
    fee: Decimal
    fee_asset: str
    ts: datetime


class Position(_Model):
    instrument: str
    side: Side
    qty: Decimal
    entry_price: Decimal
    leverage: Decimal = Decimal(1)
    unrealized_pnl: Decimal = Decimal(0)


class Balance(_Model):
    asset: str
    total: Decimal
    free: Decimal
    as_of: datetime
    stale: bool = False


class Candle(_Model):
    instrument: str
    tf: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class Trade(_Model):
    instrument: str
    ts: datetime
    price: Decimal
    qty: Decimal
    side: Side


class BookLevel(_Model):
    price: Decimal
    qty: Decimal


class Book(_Model):
    instrument: str
    ts: datetime
    bids: list[BookLevel]
    asks: list[BookLevel]


class Event(_Model):
    kind: str
    ts: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class StopSpec(_Model):
    """Стоп стратегии (G04, История 10): дневной и/или на просадку — хотя бы один.
    Действует поверх лимитов ветки, побеждает более строгий."""

    daily_pct: Decimal | None = Field(default=None, gt=0, le=100)
    max_dd_pct: Decimal | None = Field(default=None, gt=0, le=100)
    max_position_pct: Decimal | None = Field(default=None, gt=0, le=100)

    @model_validator(mode="after")
    def _at_least_one(self) -> "StopSpec":
        if self.daily_pct is None and self.max_dd_pct is None:
            raise ValueError("нужен хотя бы один стоп: daily_pct и/или max_dd_pct")
        return self


class StrategyManifest(_Model):
    """Декларативный манифест стратегии (решение §9)."""

    slug: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    branch: Branch
    venue: str = Field(min_length=1)
    source_kind: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    source_ref: str | None = None
    instruments: list[str] = Field(min_length=1)
    timeframe: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    can_backtest: bool = True
    stop: StopSpec
    valid_until: datetime | None = None
    description: str = ""

    @field_validator("valid_until")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)


class NftMint(_Model):
    collection: str
    chain: str
    market: str
    starts_at: datetime | None = None
    price: Decimal | None = None
    supply: int | None = None
    creator: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class MintAttemptSpec(_Model):
    collection: str
    chain: str
    market: str
    qty: int = Field(gt=0)
    max_price: Decimal
    priority_fee: Decimal = Decimal(0)
    mode: ModeLiteral


class MintResult(_Model):
    ok: bool
    tx_id: str | None = None
    minted: int = 0
    costs: Costs = Costs()
    detail: str = ""
