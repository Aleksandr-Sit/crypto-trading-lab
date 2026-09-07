"""База NFT-площадок: единый контракт `NftMarket` (История 81, G03).

Одна площадка — один класс. Различаются они адресами эндпоинтов и разбором ответа;
квота, здоровье, отказ read-only площадки от минта — общие. Blur сюда попадает как
`read_only=True`: публичного API у него нет, данные читаются on-chain (Alchemy),
а торговать через него система не умеет и не притворяется, что умеет.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from lab.contracts import Costs, Health, MintAttemptSpec, MintResult, NftMint
from lab.feeds import NullQuota, QuotaSink
from lab.feeds.chains.transport import HttpTransport, HttpxTransport
from lab.feeds.nft.config import MarketConfig, NftConfig, load_nft
from lab.feeds.nft.types import CollectionStats, LaunchpadSlot, Listing, NftSale


class NftError(RuntimeError):
    """Отказ площадки: нет данных, нет ключа, неподдержанный метод."""


class NftReadOnly(NftError):
    """Площадка помечена read-only — торговать через неё нельзя (Blur)."""


class NftUnsupported(NftError):
    """Метод у площадки не существует (а не «временно недоступен»)."""


class NftDisabled(NftError):
    """Площадка выключена в конфиге — например, Tensor без ключа."""


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — площадка может прислать что угодно
        return None


class NftMarketBase:
    """Общее для площадок: квота на каждый вызов, ключ из env, здоровье, издержки."""

    market: str = "nft"
    chain: str = ""
    feed_id: str = "nft"
    base: str = ""
    read_only: bool = False

    def __init__(
        self,
        transport: HttpTransport | None = None,
        *,
        quota: QuotaSink | None = None,
        api_key: str | None = None,
        base: str | None = None,
        config: NftConfig | None = None,
    ) -> None:
        self.transport = transport or HttpxTransport()
        self.quota = quota or NullQuota()
        self.api_key = api_key
        self.config = config or load_nft()
        self.base = base or self.base
        self.id = self.feed_id
        self._market_config: MarketConfig | None = self.config.markets.get(self.market)
        if self._market_config is not None:
            self.read_only = self._market_config.read_only or self.read_only

    # -- служебное ---------------------------------------------------------------------

    @property
    def weight(self) -> int:
        return self._market_config.weight if self._market_config else 1

    @property
    def enabled(self) -> bool:
        return self._market_config.enabled if self._market_config else True

    def headers(self) -> dict[str, str]:
        return {}

    def _get(self, url: str, params: dict | None = None) -> Any:
        if not self.enabled:
            raise NftDisabled(f"{self.market}: площадка выключена в config/nft.yaml")
        self.quota.use(self.feed_id, self.weight)
        return self.transport.get(url, params=params, headers=self.headers() or None)

    # -- данные ------------------------------------------------------------------------

    def collection(self, collection: str) -> CollectionStats | None:
        raise NftUnsupported(f"{self.market}: снимок коллекции не поддержан")

    def collections(self, *, limit: int = 20) -> Sequence[CollectionStats]:
        raise NftUnsupported(f"{self.market}: список коллекций не поддержан")

    def floor(self, collection: str) -> Decimal | None:
        stats = self.collection(collection)
        return stats.floor if stats else None

    def listings(self, collection: str, *, limit: int = 20) -> Sequence[Listing]:
        raise NftUnsupported(f"{self.market}: листинги не поддержаны")

    def sales(
        self,
        collection: str,
        *,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        limit: int = 50,
    ) -> Sequence[NftSale]:
        raise NftUnsupported(f"{self.market}: история продаж не поддержана")

    def holders(self, collection: str) -> int | None:
        stats = self.collection(collection)
        return stats.holders if stats else None

    def upcoming(self) -> Sequence[NftMint]:
        """Предстоящие минты. У площадки без launchpad — пусто, а не ошибка."""
        return []

    def launchpad_slot(self, collection: str) -> LaunchpadSlot | None:
        return None

    # -- торговля ----------------------------------------------------------------------

    def mint(self, spec: MintAttemptSpec) -> MintResult:
        raise NftReadOnly(
            f"{self.market}: только чтение — минт и ордера через эту площадку не идут"
        )

    def estimate_costs(self, spec: MintAttemptSpec) -> Costs:
        from lab.nft.costs import nft_costs

        price = spec.max_price * spec.qty
        return nft_costs(price, market=self.market, config=self.config, side="buy").model_copy(
            update={"priority_fee": spec.priority_fee}
        )

    # -- здоровье ----------------------------------------------------------------------

    def _ping(self) -> None:
        raise NotImplementedError

    def health(self) -> Health:
        now = datetime.now(UTC)
        if not self.enabled:
            return Health(
                status="down",
                detail=f"{self.market}: выключена в конфиге (нет ключа)",
                checked_at=now,
            )
        try:
            self._ping()
        except NotImplementedError:
            return Health(status="ok", detail=self.market, checked_at=now)
        except Exception as exc:  # noqa: BLE001 — здоровье не должно ронять вызывающего
            return Health(status="down", detail=f"{self.market}: {exc}", checked_at=now)
        return Health(status="ok", detail=self.market, checked_at=now)
