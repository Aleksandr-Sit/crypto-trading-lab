"""Площадки NFT за общим контрактом: Magic Eden, OpenSea v2, Zora, Alchemy NFT, Tensor, Blur.

Адреса и лимиты — из `research-sources.md` §6 (проверено 09.2026): Reservoir и SimpleHash
закрыты, поэтому on-chain-чтение (Blur) идёт через Alchemy, а не через агрегатор.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from lab.contracts import NftMint
from lab.feeds.chains.transport import HttpTransport
from lab.feeds.nft.base import NftDisabled, NftError, NftMarketBase, _dec
from lab.feeds.nft.config import NftConfig, load_nft
from lab.feeds.nft.opensea_key import OpenSeaKey
from lab.feeds.nft.types import CollectionStats, LaunchpadSlot, Listing, NftSale

ME_BASE = "https://api-mainnet.magiceden.dev/v2"
OS_BASE = "https://api.opensea.io/api/v2"
ZORA_BASE = "https://api.zora.co"
ALCHEMY_BASE = "https://eth-mainnet.g.alchemy.com/nft/v3"
TENSOR_BASE = "https://api.mainnet.tensordev.io/api/v1"


def _ts(value: Any) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        if isinstance(value, int | float):
            seconds = float(value)
            if seconds > 1e11:  # миллисекунды
                seconds /= 1000
            return datetime.fromtimestamp(seconds, UTC)
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, OSError, OverflowError):
        return datetime.now(UTC)


class MagicEdenMarket(NftMarketBase):
    """Magic Eden: 2 QPS / 120 QPM без ключа. Коллекции, активность, launchpad."""

    market = "magiceden"
    chain = "solana"
    feed_id = "magiceden"
    base = ME_BASE

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def collection(self, collection: str) -> CollectionStats | None:
        info = self._get(f"{self.base}/collections/{collection}") or {}
        stats = self._get(f"{self.base}/collections/{collection}/stats") or {}
        if not info and not stats:
            return None
        floor = _dec(stats.get("floorPrice"))
        return CollectionStats(
            collection=collection,
            chain=self.chain,
            market=self.market,
            name=str(info.get("name", collection)),
            floor=None if floor is None else floor / Decimal(10**9),  # лампорты → SOL
            volume_24h=_dec(stats.get("volume24hr")),
            listed=stats.get("listedCount"),
            supply=info.get("totalItems") or stats.get("supply"),
            currency="SOL",
            creator=str(info.get("creator", "")),
            ts=datetime.now(UTC),
            raw={"info": info, "stats": stats},
        )

    def listings(self, collection: str, *, limit: int = 20) -> Sequence[Listing]:
        rows = self._get(
            f"{self.base}/collections/{collection}/listings", {"offset": 0, "limit": limit}
        )
        out: list[Listing] = []
        for row in rows or []:
            price = _dec(row.get("price"))
            if price is None:
                continue
            out.append(
                Listing(
                    collection=collection,
                    token_id=str(row.get("tokenMint", "")),
                    price=price,
                    market=self.market,
                    seller=str(row.get("seller", "")),
                    ts=_ts(row.get("expiry")),
                )
            )
        return out

    def sales(
        self,
        collection: str,
        *,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 50,
    ) -> Sequence[NftSale]:
        rows = self._get(
            f"{self.base}/collections/{collection}/activities", {"offset": 0, "limit": limit}
        )
        out: list[NftSale] = []
        for row in rows or []:
            if row.get("type") not in ("buyNow", "sale"):
                continue
            price = _dec(row.get("price"))
            ts = _ts(row.get("blockTime"))
            if price is None or (from_ts and ts < from_ts) or (to_ts and ts > to_ts):
                continue
            out.append(
                NftSale(
                    collection=collection,
                    token_id=str(row.get("tokenMint", "")),
                    price=price,
                    market=self.market,
                    buyer=str(row.get("buyer", "")),
                    seller=str(row.get("seller", "")),
                    ts=ts,
                )
            )
        return out

    def upcoming(self) -> Sequence[NftMint]:
        rows = self._get(f"{self.base}/launchpad/collections", {"offset": 0, "limit": 100})
        out: list[NftMint] = []
        for row in rows or []:
            out.append(
                NftMint(
                    collection=str(row.get("symbol") or row.get("name", "")),
                    chain=self.chain,
                    market=self.market,
                    starts_at=_ts(row.get("launchDatetime")) if row.get("launchDatetime") else None,
                    price=_dec(row.get("price")),
                    supply=row.get("size"),
                    creator=str(row.get("creator", "")) or None,
                    meta={
                        "name": row.get("name", ""),
                        "source": "magiceden_launchpad",
                        "minted": row.get("minted", 0),
                        "featured": bool(row.get("featured", False)),
                    },
                )
            )
        return out

    def launchpad_slot(self, collection: str) -> LaunchpadSlot | None:
        for mint in self.upcoming():
            if mint.collection == collection:
                return LaunchpadSlot(
                    collection=collection,
                    minted=int(mint.meta.get("minted", 0) or 0),
                    supply=int(mint.supply or 0),
                )
        return None

    def _ping(self) -> None:
        self._get(f"{self.base}/collections", {"offset": 0, "limit": 1})


class OpenSeaMarket(NftMarketBase):
    """OpenSea v2. Free-ключ живёт 7 дней — `OpenSeaKey` обновляет его сам."""

    market = "opensea"
    chain = "ethereum"
    feed_id = "opensea"
    base = OS_BASE

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        key: OpenSeaKey | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(transport, **kw)
        self.key = key or OpenSeaKey(self.transport, key=self.api_key, config=self.config)

    def headers(self) -> dict[str, str]:
        value = self.key.value()
        return {"X-API-KEY": value, "accept": "application/json"} if value else {}

    def collection(self, collection: str) -> CollectionStats | None:
        info = self._get(f"{self.base}/collections/{collection}") or {}
        stats = (self._get(f"{self.base}/collections/{collection}/stats") or {}).get("total", {})
        if not info and not stats:
            return None
        return CollectionStats(
            collection=collection,
            chain=str(info.get("chain", self.chain)),
            market=self.market,
            name=str(info.get("name", collection)),
            floor=_dec(stats.get("floor_price")),
            volume_24h=_dec(stats.get("volume")),
            sales_24h=stats.get("sales"),
            holders=stats.get("num_owners"),
            supply=info.get("total_supply"),
            currency="ETH",
            creator=str(info.get("owner", "")),
            raw={"info": info, "stats": stats},
        )

    def listings(self, collection: str, *, limit: int = 20) -> Sequence[Listing]:
        payload = self._get(f"{self.base}/listings/collection/{collection}/all", {"limit": limit})
        out: list[Listing] = []
        for row in (payload or {}).get("listings", []):
            params = (row.get("price") or {}).get("current") or {}
            price = _dec(params.get("value"))
            decimals = int(params.get("decimals", 18) or 18)
            if price is None:
                continue
            params_of = (row.get("protocol_data") or {}).get("parameters") or {}
            offer = params_of.get("offer") or [{}]
            out.append(
                Listing(
                    collection=collection,
                    token_id=str(offer[0].get("identifierOrCriteria", "")),
                    price=price / Decimal(10**decimals),
                    market=self.market,
                    seller=str(params_of.get("offerer", "")),
                )
            )
        return out

    def sales(
        self,
        collection: str,
        *,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 50,
    ) -> Sequence[NftSale]:
        params: dict[str, Any] = {"event_type": "sale", "limit": limit}
        if from_ts:
            params["after"] = int(from_ts.timestamp())
        if to_ts:
            params["before"] = int(to_ts.timestamp())
        payload = self._get(f"{self.base}/events/collection/{collection}", params)
        out: list[NftSale] = []
        for row in (payload or {}).get("asset_events", []):
            price = _dec((row.get("payment") or {}).get("quantity"))
            decimals = int((row.get("payment") or {}).get("decimals", 18) or 18)
            if price is None:
                continue
            out.append(
                NftSale(
                    collection=collection,
                    token_id=str((row.get("nft") or {}).get("identifier", "")),
                    price=price / Decimal(10**decimals),
                    market=self.market,
                    buyer=str(row.get("buyer", "")),
                    seller=str(row.get("seller", "")),
                    ts=_ts(row.get("event_timestamp")),
                )
            )
        return out

    def _ping(self) -> None:
        self._get(f"{self.base}/collections", {"limit": 1})


class ZoraMarket(NftMarketBase):
    """Zora: публичный REST, ключ бесплатный."""

    market = "zora"
    chain = "base"
    feed_id = "zora"
    base = ZORA_BASE

    def collection(self, collection: str) -> CollectionStats | None:
        payload = self._get(f"{self.base}/discover/coins/{collection}") or {}
        row = payload.get("coin", payload)
        if not row:
            return None
        return CollectionStats(
            collection=collection,
            chain=str(row.get("chain", self.chain)),
            market=self.market,
            name=str(row.get("name", collection)),
            floor=_dec(row.get("floorPrice") or row.get("price")),
            volume_24h=_dec(row.get("volume24h")),
            holders=row.get("uniqueHolders"),
            supply=row.get("totalSupply"),
            currency="USD",
            creator=str(row.get("creatorAddress", "")),
            raw=row if isinstance(row, dict) else {},
        )

    def _ping(self) -> None:
        self._get(f"{self.base}/discover/coins", {"count": 1})


class AlchemyNftMarket(NftMarketBase):
    """Alchemy NFT API — on-chain-чтение: держатели, метаданные, флор по контракту."""

    market = "alchemy_nft"
    chain = "ethereum"
    feed_id = "alchemy_nft"
    base = ALCHEMY_BASE
    read_only = True

    def _url(self, path: str) -> str:
        key = self.api_key or "demo"
        return f"{self.base}/{key}/{path}"

    def collection(self, collection: str) -> CollectionStats | None:
        meta = self._get(self._url("getContractMetadata"), {"contractAddress": collection}) or {}
        floors = self._get(self._url("getFloorPrice"), {"contractAddress": collection}) or {}
        if not meta and not floors:
            return None
        opensea = floors.get("openSea") or {}
        return CollectionStats(
            collection=collection,
            chain=self.chain,
            market=self.market,
            name=str(meta.get("name", collection)),
            floor=_dec(opensea.get("floorPrice")),
            supply=_int(meta.get("totalSupply")),
            currency=str(opensea.get("priceCurrency", "ETH")),
            raw={"meta": meta, "floors": floors},
        )

    def holders(self, collection: str) -> int | None:
        payload = (
            self._get(self._url("getOwnersForContract"), {"contractAddress": collection}) or {}
        )
        owners = payload.get("owners")
        return len(owners) if isinstance(owners, list) else None

    def _ping(self) -> None:
        self._get(self._url("getContractMetadata"), {"contractAddress": "0x0"})


class BlurMarket(AlchemyNftMarket):
    """Blur — только чтение on-chain (История 81): публичного API нет, торговли нет."""

    market = "blur"
    chain = "ethereum"
    feed_id = "blur"
    read_only = True


class TensorMarket(NftMarketBase):
    """Tensor Alpha REST — только по ключу (заявка). Без ключа площадка выключена."""

    market = "tensor"
    chain = "solana"
    feed_id = "tensor"
    base = TENSOR_BASE

    def headers(self) -> dict[str, str]:
        return {"x-tensor-api-key": self.api_key} if self.api_key else {}

    def _get(self, url: str, params: dict | None = None) -> Any:
        if not self.api_key:
            raise NftDisabled("tensor: ключ выдаётся по заявке — площадка недоступна")
        return super()._get(url, params)

    def collection(self, collection: str) -> CollectionStats | None:
        payload = self._get(f"{self.base}/collections", {"slugs": collection}) or {}
        rows = payload.get("collections") or []
        if not rows:
            return None
        row = rows[0]
        return CollectionStats(
            collection=collection,
            chain=self.chain,
            market=self.market,
            name=str(row.get("name", collection)),
            floor=_dec(row.get("statsV2", {}).get("buyNowPrice")),
            volume_24h=_dec(row.get("statsV2", {}).get("volume24h")),
            listed=row.get("statsV2", {}).get("numListed"),
            currency="SOL",
            raw=row,
        )

    def _ping(self) -> None:
        self._get(f"{self.base}/collections", {"limit": 1})


def _int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


MARKETS: dict[str, type[NftMarketBase]] = {
    "magiceden": MagicEdenMarket,
    "opensea": OpenSeaMarket,
    "zora": ZoraMarket,
    "alchemy_nft": AlchemyNftMarket,
    "tensor": TensorMarket,
    "blur": BlurMarket,
}

READ_ONLY_MARKETS: tuple[str, ...] = ("blur", "alchemy_nft")


def make_market(
    market: str,
    transport: HttpTransport | None = None,
    *,
    quota: Any = None,
    api_key: str | None = None,
    config: NftConfig | None = None,
    env: dict[str, str] | None = None,
) -> NftMarketBase:
    """Площадка по имени. Ключ — из `.env` по имени из конфига, если не передан явно."""
    cfg = config or load_nft()
    cls = MARKETS.get(market)
    if cls is None:
        raise NftError(f"неизвестная NFT-площадка: {market}")
    spec = cfg.markets.get(market)
    key = api_key
    if key is None and spec is not None and spec.key_env:
        import os

        source = env if env is not None else os.environ
        key = source.get(spec.key_env) or None
    instance = cls(transport, quota=quota, api_key=key, config=cfg)
    if spec is not None and not spec.enabled and not key:
        raise NftDisabled(f"{market}: выключена в config/nft.yaml и ключа нет")
    if spec is not None and not spec.enabled and key:
        instance._market_config = spec.model_copy(update={"enabled": True})
    return instance
