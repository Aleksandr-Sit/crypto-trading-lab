"""Типы NFT-слоя: снимок коллекции, листинг, продажа, история коллекции создателя.

Инструмент NFT-ордера — `<коллекция>` для флор-покупки и `<коллекция>:<token_id>` для
конкретного предмета: так исполнителю не нужен отдельный словарь идентификаторов.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any


def _now() -> datetime:
    return datetime.now(UTC)


def instrument_of(collection: str, token_id: str | None = None) -> str:
    return f"{collection}:{token_id}" if token_id else collection


def split_instrument(instrument: str) -> tuple[str, str | None]:
    collection, _, token_id = instrument.partition(":")
    return collection, token_id or None


@dataclass(frozen=True)
class CollectionStats:
    """Снимок коллекции: флор, объём, листинги, продажи, держатели (История 72)."""

    collection: str
    chain: str = ""
    market: str = ""
    name: str = ""
    floor: Decimal | None = None
    volume_24h: Decimal | None = None
    listed: int | None = None
    holders: int | None = None
    supply: int | None = None
    sales_24h: int | None = None
    currency: str = "USD"
    creator: str = ""
    ts: datetime = field(default_factory=_now)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Listing:
    collection: str
    token_id: str
    price: Decimal
    market: str = ""
    seller: str = ""
    ts: datetime = field(default_factory=_now)

    @property
    def instrument(self) -> str:
        return instrument_of(self.collection, self.token_id)


@dataclass(frozen=True)
class NftSale:
    collection: str
    token_id: str
    price: Decimal
    market: str = ""
    buyer: str = ""
    seller: str = ""
    ts: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class CollectionHistory:
    """Прошлая коллекция создателя: цена минта и флор через 1/7/30 дней (История 73)."""

    collection: str
    creator: str
    minted_at: datetime
    mint_price: Decimal
    floor_1d: Decimal | None = None
    floor_7d: Decimal | None = None
    floor_30d: Decimal | None = None
    chain: str = ""
    market: str = ""

    def floor_at(self, days: int) -> Decimal | None:
        return {1: self.floor_1d, 7: self.floor_7d, 30: self.floor_30d}.get(days)

    def ratio_at(self, days: int) -> Decimal | None:
        floor = self.floor_at(days)
        if floor is None or self.mint_price <= 0:
            return None
        return floor / self.mint_price


@dataclass(frozen=True)
class LaunchpadSlot:
    """Заполненность слота launchpad — компонент индекса внимания."""

    collection: str
    minted: int = 0
    supply: int = 0
    allowlist_seats: int | None = None
    allowlist_demand: int | None = None

    @property
    def fill(self) -> Decimal:
        if self.supply <= 0:
            return Decimal(0)
        return Decimal(self.minted) / Decimal(self.supply)
