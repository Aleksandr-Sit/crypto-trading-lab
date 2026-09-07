"""Трекер коллекций: снимки флора/объёма/листингов/продаж/держателей (История 72, R18).

Одна коллекция может жить на нескольких площадках — снимок делается по каждой, у которой
она есть, и хранится с именем площадки. Флор берём минимальный из живых: это цена, по
которой предмет действительно можно купить прямо сейчас.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from lab.feeds.nft.base import NftError, NftMarketBase
from lab.feeds.nft.config import NftConfig, load_nft
from lab.feeds.nft.types import CollectionStats, Listing, NftSale
from lab.nft.store import CollectionStore, sales_per_minute


@dataclass(frozen=True)
class TrackResult:
    collection: str
    snapshots: list[CollectionStats]
    errors: dict[str, str]

    @property
    def floor(self) -> Decimal | None:
        floors = [s.floor for s in self.snapshots if s.floor is not None]
        return min(floors) if floors else None

    @property
    def ok(self) -> bool:
        return bool(self.snapshots)


class CollectionTracker:
    """`track(collection)` из границ модуля `nft` (interfaces.md)."""

    def __init__(
        self,
        markets: Sequence[NftMarketBase],
        *,
        store: CollectionStore | None = None,
        config: NftConfig | None = None,
        clock=None,
    ) -> None:
        self.markets = list(markets)
        self.store = store if store is not None else CollectionStore()
        self.config = config or load_nft()
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        return self._clock()

    def _markets_for(self, market: str | None) -> list[NftMarketBase]:
        if market is None:
            return self.markets
        return [m for m in self.markets if m.market == market]

    def track(
        self, collection: str, *, market: str | None = None, now: datetime | None = None
    ) -> TrackResult:
        """Снимок по всем площадкам, где коллекция есть, и запись в историю."""
        at = now or self._now()
        snapshots: list[CollectionStats] = []
        errors: dict[str, str] = {}
        for source in self._markets_for(market):
            try:
                stats = source.collection(collection)
            except NftError as exc:
                errors[source.market] = str(exc)
                continue
            except Exception as exc:  # noqa: BLE001 — одна площадка не роняет остальные
                errors[source.market] = str(exc)
                continue
            if stats is None:
                continue
            snapshots.append(
                CollectionStats(
                    **{**stats.__dict__, "ts": at, "market": stats.market or source.market}
                )
            )
        if snapshots and self.store is not None:
            self.store.write(snapshots)
        return TrackResult(collection=collection, snapshots=snapshots, errors=errors)

    def track_all(self, collections: Sequence[str], **kw) -> list[TrackResult]:
        return [self.track(c, **kw) for c in collections]

    # -- чтение истории -----------------------------------------------------------------

    def history(self, collection: str, **kw) -> list[CollectionStats]:
        return self.store.read(collection, **kw) if self.store is not None else []

    def floor(self, collection: str, *, market: str | None = None) -> Decimal | None:
        """Живой флор — минимальный по площадкам; ни одна не ответила → из истории."""
        floors: list[Decimal] = []
        for source in self._markets_for(market):
            try:
                value = source.floor(collection)
            except Exception:  # noqa: BLE001
                continue
            if value is not None:
                floors.append(value)
        if floors:
            return min(floors)
        last = self.store.last(collection) if self.store is not None else None
        return last.floor if last else None

    def listings(
        self, collection: str, *, market: str | None = None, limit: int = 20
    ) -> list[Listing]:
        out: list[Listing] = []
        for source in self._markets_for(market):
            try:
                out.extend(source.listings(collection, limit=limit))
            except Exception:  # noqa: BLE001 — листингов может не быть у площадки
                continue
        return sorted(out, key=lambda listing: listing.price)

    def sales(
        self,
        collection: str,
        *,
        market: str | None = None,
        window_min: int | None = None,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[NftSale]:
        at = now or self._now()
        from_ts = at - timedelta(minutes=window_min) if window_min else None
        out: list[NftSale] = []
        for source in self._markets_for(market):
            try:
                out.extend(source.sales(collection, from_ts=from_ts, to_ts=at, limit=limit))
            except Exception:  # noqa: BLE001
                continue
        return sorted(out, key=lambda sale: sale.ts)

    def sales_rate(
        self, collection: str, *, window_min: int | None = None, now: datetime | None = None
    ) -> Decimal:
        window = window_min or self.config.secondary.window_min
        at = now or self._now()
        return sales_per_minute(
            self.sales(collection, window_min=window, now=at), window_min=window, now=at
        )

    def last_sale_at(self, collection: str, *, days: int = 30, now: datetime | None = None):
        at = now or self._now()
        sales = self.sales(collection, window_min=days * 24 * 60, now=at)
        return sales[-1].ts if sales else None

    def tracked(self) -> list[str]:
        return self.store.collections() if self.store is not None else []
