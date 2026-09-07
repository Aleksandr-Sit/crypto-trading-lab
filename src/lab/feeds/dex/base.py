"""База DEX-фидов: контракт `Feed` целиком, свечей и стакана у пула нет."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from lab.contracts import Book, Candle, Event, Health, Trade
from lab.feeds import NullQuota, QuotaSink
from lab.feeds.chains.transport import ChainError, ChainUnsupported, HttpTransport


class DexFeed:
    """Общее: квота на каждый сетевой вызов, отказ по неподдерживаемым методам `Feed`."""

    feed_id: str = "dex"
    chain: str = ""
    weight: int = 1

    def __init__(self, transport: HttpTransport, *, quota: QuotaSink | None = None) -> None:
        self.transport = transport
        self.quota = quota or NullQuota()
        self.id = self.feed_id

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None):
        self.quota.use(self.feed_id, self.weight)
        return self.transport.get(url, params=params, headers=headers)

    # -- контракт Feed ------------------------------------------------------------------

    def candles(
        self, instrument: str, tf: str, from_ts: datetime, to_ts: datetime
    ) -> Sequence[Candle]:
        raise ChainUnsupported(
            f"{self.feed_id}: OHLCV бесплатно не отдаёт — свечи ранней стадии не строим"
        )

    def trades(self, instrument: str, from_ts: datetime, to_ts: datetime) -> Sequence[Trade]:
        raise ChainUnsupported(f"{self.feed_id}: истории сделок нет — только снимки пары")

    def book(self, instrument: str) -> Book:
        raise ChainUnsupported(f"{self.feed_id}: стакана нет — ликвидность живёт в пуле")

    async def events(self, kind: str, **kw) -> AsyncIterator[Event]:
        raise ChainUnsupported(f"{self.feed_id}: события {kind!r} не поддержаны")
        yield  # pragma: no cover — делает метод асинхронным генератором

    def health(self) -> Health:
        now = datetime.now(UTC)
        try:
            self._ping()
        except ChainError as err:
            return Health(status="down", detail=f"{self.feed_id}: {err}", checked_at=now)
        return Health(status="ok", detail=self.feed_id, checked_at=now)

    def _ping(self) -> None:
        return None


__all__ = ["DexFeed"]
