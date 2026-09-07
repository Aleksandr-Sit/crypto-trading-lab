"""Типы ранней стадии: новый токен, миграция, пара, снимок токена.

Один тип `TokenInfo` на все сети: чек-лист честности и фильтр потока смотрят на
одни и те же признаки, откуда бы они ни приехали — PumpPortal, DexScreener, STON.fi.
Поля, которых у сети нет, остаются `None` — и это видно в чек-листе как «нет данных»,
а не как «проверка пройдена».
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NEW_TOKEN_EVENT = "new_token"
MIGRATION_EVENT = "migration"
NEW_PAIR_EVENT = "new_pair"

Source = Literal["pumpportal", "dexscreener", "stonfi"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TokenInfo(_Model):
    """Снимок токена на момент оценки: цифры честности и ликвидности вместе."""

    address: str
    chain: str
    symbol: str = ""
    name: str = ""
    created_at: datetime | None = None
    creator: str | None = None
    source: str = ""
    pair: str = ""
    venue: str = ""
    price_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    buys: int | None = None
    sells: int | None = None
    holders: int | None = None
    top_holders: list[Decimal] = Field(default_factory=list)  # доли в %, по убыванию
    mint_authority: str | None = None
    freeze_authority: str | None = None
    lp_locked: bool | None = None
    migrated: bool = False
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.chain}:{self.address}"

    def age_s(self, now: datetime | None = None) -> Decimal | None:
        if self.created_at is None:
            return None
        now = now or datetime.now(UTC)
        return Decimal(str((now - self.created_at).total_seconds()))

    def top1_pct(self) -> Decimal | None:
        return max(self.top_holders) if self.top_holders else None

    def top10_pct(self) -> Decimal | None:
        if not self.top_holders:
            return None
        return sum(sorted(self.top_holders, reverse=True)[:10], Decimal(0))


def token_payload(token: TokenInfo) -> dict[str, Any]:
    """Payload события потока — вход стратегий `meme-*` (ключи стабильны)."""
    return {
        "address": token.address,
        "chain": token.chain,
        "symbol": token.symbol,
        "source": token.source,
        "venue": token.venue,
        "pair": token.pair,
        "price_usd": None if token.price_usd is None else str(token.price_usd),
        "liquidity_usd": None if token.liquidity_usd is None else str(token.liquidity_usd),
        "volume_usd": None if token.volume_usd is None else str(token.volume_usd),
        "buys": token.buys,
        "migrated": token.migrated,
        "created_at": None if token.created_at is None else token.created_at.isoformat(),
        "token": token.model_dump(mode="json"),
    }


__all__ = [
    "MIGRATION_EVENT",
    "NEW_PAIR_EVENT",
    "NEW_TOKEN_EVENT",
    "Source",
    "TokenInfo",
    "token_payload",
]
