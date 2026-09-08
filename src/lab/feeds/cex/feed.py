"""CexFeed — контракт Feed поверх ccxt-транспорта: свечи, сделки, стакан, фандинг, health.

Один класс на все четыре площадки, различия — в `VenueSpec` (размер страницы, вес запроса).
Каждый сетевой вызов проходит через `quota.use(feed_id, weight)`.
`source()` отдаёт функцию `(instr, tf, from, to) -> [Candle]` для `lab.data.backfill`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import ccxt

from lab.contracts import Book, BookLevel, Candle, Event, Health, Trade
from lab.contracts.timeframes import parse_tf
from lab.feeds import NullQuota, QuotaSink
from lab.feeds.cex.transport import VENUE_SPECS, Transport, VenueSpec

_GEO_MARKERS = ("restricted location", "restricted", "unavailable in your region", "geo", "403")


def _dec(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _ts(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


@dataclass(frozen=True)
class FundingRate:
    instrument: str
    rate: Decimal
    next_at: datetime | None
    mark_price: Decimal | None


class CexFeed:
    spec: VenueSpec

    def __init__(
        self,
        transport: Transport,
        *,
        quota: QuotaSink | None = None,
        page_limit: int | None = None,
        degraded_after_s: float = 2.0,
    ) -> None:
        self.spec = VENUE_SPECS[transport.id]
        self.id = self.spec.id
        self.venue = self.spec.id
        self.transport = transport
        self.quota = quota or NullQuota()
        self.page_limit = page_limit or self.spec.page_limit
        self.degraded_after_s = degraded_after_s

    # -- квота ------------------------------------------------------------------

    def _call(self, name: str, *args: Any, **kw: Any) -> Any:
        self.quota.use(self.id, self.spec.weight)
        return getattr(self.transport, name)(*args, **kw)

    # -- Feed -------------------------------------------------------------------

    def candles(
        self, instrument: str, tf: str, from_ts: datetime, to_ts: datetime
    ) -> Sequence[Candle]:
        step = parse_tf(tf)
        from_ts, to_ts = from_ts.astimezone(UTC), to_ts.astimezone(UTC)
        out: list[Candle] = []
        cursor = int(from_ts.timestamp() * 1000)
        end = int(to_ts.timestamp() * 1000)
        while cursor < end:
            rows = self._call("fetch_ohlcv", instrument, tf, cursor, self.page_limit)
            rows = [r for r in rows if cursor <= r[0] < end]
            if not rows:
                break
            out.extend(
                Candle(
                    instrument=instrument,
                    tf=tf,
                    ts=_ts(r[0]),
                    open=_dec(r[1]),
                    high=_dec(r[2]),
                    low=_dec(r[3]),
                    close=_dec(r[4]),
                    volume=_dec(r[5]),
                )
                for r in rows
            )
            cursor = rows[-1][0] + int(step.total_seconds() * 1000)
        return out

    def source(self) -> Callable[[str, str, datetime, datetime], Sequence[Candle]]:
        return self.candles

    def trades(self, instrument: str, from_ts: datetime, to_ts: datetime) -> Sequence[Trade]:
        since = int(from_ts.timestamp() * 1000)
        end = int(to_ts.timestamp() * 1000)
        out: list[Trade] = []
        while True:
            rows = self._call("fetch_trades", instrument, since, self.page_limit)
            rows = [r for r in rows if since <= r["timestamp"] < end]
            if not rows:
                break
            out.extend(
                Trade(
                    instrument=instrument,
                    ts=_ts(r["timestamp"]),
                    price=_dec(r["price"]),
                    qty=_dec(r["amount"]),
                    side=r["side"],
                )
                for r in rows
            )
            last = rows[-1]["timestamp"]
            if last <= since:
                break
            since = last + 1
        return out

    def book(self, instrument: str, depth: int = 50) -> Book:
        raw = self._call("fetch_order_book", instrument, depth)
        ts = raw.get("timestamp") or int(time.time() * 1000)
        return Book(
            instrument=instrument,
            ts=_ts(ts),
            bids=[BookLevel(price=_dec(p), qty=_dec(q)) for p, q, *_ in raw["bids"]],
            asks=[BookLevel(price=_dec(p), qty=_dec(q)) for p, q, *_ in raw["asks"]],
        )

    def ticker(self, instrument: str) -> dict[str, Decimal]:
        raw = self._call("fetch_ticker", instrument)
        return {k: _dec(raw.get(k)) for k in ("bid", "ask", "last")}

    def funding(self, instrument: str) -> FundingRate:
        raw = self._call("fetch_funding_rate", instrument)
        next_ms = raw.get("fundingTimestamp") or raw.get("nextFundingTimestamp")
        mark = raw.get("markPrice")
        return FundingRate(
            instrument=instrument,
            rate=_dec(raw.get("fundingRate")),
            next_at=_ts(next_ms) if next_ms else None,
            mark_price=_dec(mark) if mark is not None else None,
        )

    async def events(
        self, kind: str, *, instruments: Sequence[str] = (), poll_s: float = 60.0
    ) -> AsyncIterator[Event]:
        """Поток событий опросом REST: kind = funding | ticker. Прерывается отменой задачи."""
        if kind not in ("funding", "ticker"):
            raise ValueError(f"неизвестный вид событий: {kind}")
        while True:
            for instrument in instruments:
                if kind == "funding":
                    f = self.funding(instrument)
                    payload = {
                        "instrument": instrument,
                        "rate": str(f.rate),
                        "next_at": f.next_at.isoformat() if f.next_at else None,
                    }
                else:
                    payload = {
                        "instrument": instrument,
                        **{k: str(v) for k, v in self.ticker(instrument).items()},
                    }
                yield Event(kind=kind, ts=datetime.now(UTC), payload=payload)
            await asyncio.sleep(poll_s)

    def health(self) -> Health:
        started = time.monotonic()
        now = datetime.now(UTC)
        try:
            self._call("fetch_time")
        except ccxt.PermissionDenied as err:
            return Health(
                status="down", detail=f"гео-блок или запрет доступа: {err}", checked_at=now
            )
        except ccxt.NetworkError as err:
            text = str(err).lower()
            if any(m in text for m in _GEO_MARKERS):
                return Health(
                    status="down", detail=f"гео-блок или недоступность: {err}", checked_at=now
                )
            return Health(status="down", detail=f"нет связи: {err}", checked_at=now)
        except ccxt.BaseError as err:
            return Health(status="down", detail=f"ошибка площадки: {err}", checked_at=now)
        latency = time.monotonic() - started
        if latency > self.degraded_after_s:
            return Health(
                status="degraded", detail=f"медленный ответ: {latency:.1f}s", checked_at=now
            )
        return Health(status="ok", detail=self.spec.rate_limit_note, checked_at=now)


class BybitFeed(CexFeed):
    pass


class OkxFeed(CexFeed):
    pass


class BinanceFeed(CexFeed):
    pass


class HyperliquidFeed(CexFeed):
    pass


class BitstampFeed(CexFeed):
    """Только чтение истории: ряд BTC/USD с 2011 года, торговых ключей у площадки нет."""


FEEDS: dict[str, type[CexFeed]] = {
    "bybit": BybitFeed,
    "okx": OkxFeed,
    "binance": BinanceFeed,
    "hyperliquid": HyperliquidFeed,
    "bitstamp": BitstampFeed,
}


def make_feed(
    venue: str, transport: Transport | None = None, *, quota: QuotaSink | None = None
) -> CexFeed:
    if transport is None:
        from lab.feeds.cex.transport import CcxtTransport

        transport = CcxtTransport(venue)
    return FEEDS[venue](transport, quota=quota)


__all__ = [
    "FEEDS",
    "BinanceFeed",
    "BitstampFeed",
    "BybitFeed",
    "CexFeed",
    "FundingRate",
    "HyperliquidFeed",
    "OkxFeed",
    "make_feed",
]
