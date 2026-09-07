"""Фейковая NFT-площадка для тестов: сеть не трогается, поведение задаётся вызывающим."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from lab.contracts import Costs, Health, MintAttemptSpec, MintResult, NftMint
from lab.feeds.nft.base import NftMarketBase, NftReadOnly
from lab.feeds.nft.types import CollectionStats, LaunchpadSlot, Listing, NftSale


class FakeNftMarket(NftMarketBase):
    """Площадка в памяти. `mint_outcomes` — сценарий попыток минта, по одному на попытку."""

    market = "fake_nft"
    chain = "solana"
    feed_id = "fake_nft"

    def __init__(
        self,
        *,
        market: str = "fake_nft",
        chain: str = "solana",
        read_only: bool = False,
        **kw,
    ) -> None:
        super().__init__(transport=_NoTransport(), **kw)
        self.market = market
        self.chain = chain
        self.read_only = read_only
        self._collections: dict[str, CollectionStats] = {}
        self._listings: dict[str, list[Listing]] = {}
        self._sales: dict[str, list[NftSale]] = {}
        self._upcoming: list[NftMint] = []
        self._slots: dict[str, LaunchpadSlot] = {}
        self.mint_outcomes: list[MintResult] = []
        self.mint_calls: list[MintAttemptSpec] = []
        self.health_status = "ok"

    # -- засев -------------------------------------------------------------------------

    def seed_collection(self, stats: CollectionStats) -> None:
        self._collections[stats.collection] = stats

    def seed_floor(self, collection: str, floor: Decimal, **kw) -> None:
        self.seed_collection(
            CollectionStats(collection=collection, market=self.market, chain=self.chain,
                            floor=floor, **kw)
        )

    def seed_listings(self, collection: str, listings: Sequence[Listing]) -> None:
        self._listings[collection] = list(listings)

    def seed_sales(self, collection: str, sales: Sequence[NftSale]) -> None:
        self._sales[collection] = list(sales)

    def seed_upcoming(self, mints: Sequence[NftMint]) -> None:
        self._upcoming = list(mints)

    def seed_slot(self, slot: LaunchpadSlot) -> None:
        self._slots[slot.collection] = slot

    # -- контракт ----------------------------------------------------------------------

    def collection(self, collection: str) -> CollectionStats | None:
        self.quota.use(self.feed_id, self.weight)
        return self._collections.get(collection)

    def collections(self, *, limit: int = 20) -> Sequence[CollectionStats]:
        return list(self._collections.values())[:limit]

    def listings(self, collection: str, *, limit: int = 20) -> Sequence[Listing]:
        self.quota.use(self.feed_id, self.weight)
        return self._listings.get(collection, [])[:limit]

    def sales(self, collection: str, *, from_ts=None, to_ts=None, limit: int = 50):
        self.quota.use(self.feed_id, self.weight)
        rows = self._sales.get(collection, [])
        if from_ts:
            rows = [s for s in rows if s.ts >= from_ts]
        if to_ts:
            rows = [s for s in rows if s.ts <= to_ts]
        return rows[:limit]

    def upcoming(self) -> Sequence[NftMint]:
        self.quota.use(self.feed_id, self.weight)
        return list(self._upcoming)

    def launchpad_slot(self, collection: str) -> LaunchpadSlot | None:
        return self._slots.get(collection)

    def mint(self, spec: MintAttemptSpec) -> MintResult:
        if self.read_only:
            raise NftReadOnly(f"{self.market}: только чтение")
        self.mint_calls.append(spec)
        if self.mint_outcomes:
            return self.mint_outcomes.pop(0)
        return MintResult(
            ok=True,
            tx_id=f"tx-{len(self.mint_calls)}",
            minted=spec.qty,
            costs=Costs(gas=Decimal("0.01"), priority_fee=spec.priority_fee),
        )

    def health(self) -> Health:
        return Health(status=self.health_status, detail=self.market, checked_at=datetime.now(UTC))


class _NoTransport:
    def get(self, url, *, params=None, headers=None):  # pragma: no cover — сеть запрещена
        raise AssertionError("фейковая площадка не ходит в сеть")

    def post(self, url, *, json=None, headers=None):  # pragma: no cover
        raise AssertionError("фейковая площадка не ходит в сеть")
