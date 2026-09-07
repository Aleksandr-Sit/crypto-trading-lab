"""База сетевых клиентов: квота на каждый вызов, `Feed` целиком, сделки кошелька.

Свечей и стакана у ончейн-клиента нет — эти методы контракта поднимают `ChainUnsupported`
(честнее пустого списка: молчаливая пустота выглядит как «данных нет», а не «не умеем»).
`trades(address, ...)` отдаёт свопы адреса в терминах `contracts.Trade`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

from lab.contracts import Book, Candle, Event, Health, Trade
from lab.feeds import NullQuota, QuotaSink
from lab.feeds.chains.config import ChainsConfig, ChainSpec, load_chains
from lab.feeds.chains.transport import ChainError, ChainUnsupported, HttpTransport
from lab.feeds.chains.types import WalletTrade

WALLET_TRADE_EVENT = "wallet_trade"


class ChainFeed:
    """Общий каркас: `chain` — имя сети, `feed_id` — ключ квоты (у BNB и EVM он общий)."""

    chain: str = ""
    config_key: str = ""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        api_key: str | None = None,
        quota: QuotaSink | None = None,
        config: ChainsConfig | None = None,
    ) -> None:
        self.transport = transport
        self.api_key = api_key
        self.quota = quota or NullQuota()
        self.config = config or load_chains()
        self.spec: ChainSpec = self.config.spec(self.config_key)
        self.feed_id = self.spec.feed_id
        self.id = self.spec.feed_id

    # -- сеть под квотой ------------------------------------------------------------

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        self.quota.use(self.feed_id, self.spec.weight)
        return self.transport.get(url, params=params, headers=headers)

    def _post(self, url: str, payload: dict, headers: dict | None = None) -> Any:
        self.quota.use(self.feed_id, self.spec.weight)
        return self.transport.post(url, json=payload, headers=headers)

    # -- сделки кошелька ------------------------------------------------------------

    def wallet_trades(
        self,
        address: str,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        *,
        limit: int = 100,
    ) -> list[WalletTrade]:
        raise NotImplementedError

    # -- контракт Feed ---------------------------------------------------------------

    def candles(
        self, instrument: str, tf: str, from_ts: datetime, to_ts: datetime
    ) -> Sequence[Candle]:
        raise ChainUnsupported(f"{self.chain}: свечей у ончейн-клиента нет — бери их у DEX-фида")

    def trades(self, instrument: str, from_ts: datetime, to_ts: datetime) -> Sequence[Trade]:
        """`instrument` здесь — адрес кошелька: свопы адреса в общем виде."""
        return [
            Trade(instrument=t.token, ts=t.ts, price=t.price, qty=t.qty, side=t.side)
            for t in self.wallet_trades(instrument, from_ts, to_ts)
        ]

    def book(self, instrument: str) -> Book:
        raise ChainUnsupported(f"{self.chain}: стакана нет — ликвидность живёт в пуле")

    async def events(
        self, kind: str = WALLET_TRADE_EVENT, *, addresses: Sequence[str] = (), poll_s: float = 15.0
    ) -> AsyncIterator[Event]:
        """Поток сделок отслеживаемых кошельков опросом (WS-подписки — за рамками таска)."""
        if kind != WALLET_TRADE_EVENT:
            raise ValueError(f"{self.chain}: неизвестный вид событий {kind!r}")
        seen: set[str] = set()
        while True:
            for address in addresses:
                for trade in self.wallet_trades(address):
                    key = f"{trade.tx}:{trade.token}:{trade.side}"
                    if key in seen:
                        continue
                    seen.add(key)
                    yield Event(kind=kind, ts=trade.ts, payload=wallet_trade_payload(trade))
            await asyncio.sleep(poll_s)

    def health(self) -> Health:
        now = datetime.now(UTC)
        if self.spec.key_env and not self.api_key:
            return Health(
                status="down",
                detail=f"нет ключа {self.spec.key_env}: сеть {self.chain} спит",
                checked_at=now,
            )
        try:
            self._ping()
        except ChainError as err:
            return Health(status="down", detail=f"{self.chain}: {err}", checked_at=now)
        return Health(status="ok", detail=self.spec.note, checked_at=now)

    def _ping(self) -> None:
        raise NotImplementedError


def wallet_trade_payload(trade: WalletTrade) -> dict[str, Any]:
    """Событие сделки лидера — вход стратегии `copy-*` (ключи стабильны, на них смотрят тесты)."""
    return {
        "chain": trade.chain,
        "address": trade.address,
        "tx": trade.tx,
        "token": trade.token,
        "symbol": trade.symbol,
        "side": trade.side,
        "qty": str(trade.qty),
        "price": str(trade.price),
        "quote_asset": trade.quote_asset,
        "quote_qty": str(trade.quote_qty),
        "value_usd": None if trade.value_usd is None else str(trade.value_usd),
        "ts": trade.ts.isoformat(),
        "venue": trade.venue,
    }


__all__ = ["WALLET_TRADE_EVENT", "ChainFeed", "wallet_trade_payload"]
