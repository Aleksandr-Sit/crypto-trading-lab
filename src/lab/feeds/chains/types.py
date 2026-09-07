"""Сделка кошелька — общий тип для всех сетей и для `lab.wallets`.

Цены сетевых свопов приходят в валюте пары (`quote_asset`), а не в USD: перевод в USD
делаем только там, где котируемая сторона — стейбл. Где не делаем — `value_usd is None`,
и статистика считается в единицах `quote_asset` (см. `WalletStats.currency`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

Side = Literal["buy", "sell"]

STABLES = frozenset({"USDC", "USDT", "DAI", "USDBC", "USDE", "FDUSD", "BUSD"})


@dataclass(frozen=True)
class WalletTrade:
    """Один своп/филл кошелька. `qty` — в токене, `quote_qty` — в валюте пары."""

    chain: str
    address: str
    tx: str
    ts: datetime
    token: str
    symbol: str
    side: Side
    qty: Decimal
    quote_asset: str
    quote_qty: Decimal
    value_usd: Decimal | None = None
    fee: Decimal = Decimal(0)
    pnl: Decimal | None = None
    venue: str = ""

    @property
    def price(self) -> Decimal:
        return self.quote_qty / self.qty if self.qty else Decimal(0)

    @property
    def value(self) -> Decimal:
        """Размер сделки в учётной валюте: USD, если цена известна, иначе — в quote."""
        return self.value_usd if self.value_usd is not None else self.quote_qty


__all__ = ["STABLES", "Side", "WalletTrade"]
