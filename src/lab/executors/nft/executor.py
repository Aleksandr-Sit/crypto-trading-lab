"""NFT-исполнитель за контрактом `Executor`: покупка по флору и листинг на продажу.

Режим задаётся экземпляру (как в `executors.cex`, `executors.dex`, `executors.polymarket`):
бумажный считает позиции у себя, живой отправляет транзакции; ордер с чужим режимом
отклоняется. Инструмент — `коллекция` (покупка по флору) или `коллекция:token_id`.

Издержки считаются по компонентам (История 79): покупателю — газ, продавцу — роялти
создателю и комиссия площадки. Покупка дороже флора на заданную премию не отправляется:
на ранней вторичке «возьмём чуть дороже» — это и есть способ купить у бота его выход.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count

from lab.contracts import (
    Balance,
    Costs,
    Fill,
    Health,
    KeyRights,
    ModeLiteral,
    Order,
    OrderIntent,
    OrderState,
    Position,
)
from lab.core.costs import CostModel, default_model
from lab.feeds import NullQuota, QuotaSink
from lab.feeds.nft.base import NftMarketBase
from lab.feeds.nft.config import NftConfig, load_nft
from lab.feeds.nft.types import split_instrument
from lab.nft.costs import nft_costs

HUNDRED = Decimal(100)


class NftError(RuntimeError):
    """Отказ NFT-исполнителя: чужой режим, read-only площадка, неизвестный ордер."""


class NotConnected(NftError):
    """Нет ключа горячего кошелька — живая покупка или минт невозможны."""


class TradingUnavailable(NftError):
    """Нет клиента подписи (solders / web3) — ветка идёт в режиме «только замер»."""


class PaperFill(Fill):
    """Бумажный филл плюс цена-ориентир на момент решения (для `Journal.record_fill`)."""

    ref_price: Decimal


class NftExecutor:
    """`market=None` — бумага по цене заявки: без сети и без котировок."""

    venue: str = "nft"
    chain: str = ""
    branch = "nft"
    quote_asset: str = "USD"
    key_env: str = ""

    def __init__(
        self,
        market: NftMarketBase | None = None,
        *,
        mode: ModeLiteral = "paper",
        costs: CostModel | None = None,
        quota: QuotaSink | None = None,
        config: NftConfig | None = None,
        paper_balance: Decimal = Decimal(10000),
        private_key: str | None = None,
        client=None,
    ) -> None:
        self.market = market
        self.mode: ModeLiteral = mode
        self.costs = costs or default_model()
        self.quota = quota or NullQuota()
        self.config = config or load_nft()
        self.private_key = private_key
        self.client = client
        self._ids = count(1)
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._positions: dict[str, Position] = {}
        self._costs: dict[str, Costs] = {}
        self._cash = paper_balance
        if market is not None and getattr(market, "read_only", False):
            raise NftError(f"{market.market}: площадка только для чтения — исполнителя нет")

    # -- ключи --------------------------------------------------------------------------

    def rights(self) -> KeyRights:
        return KeyRights(trade=bool(self.private_key), withdraw=False)

    # -- цена ---------------------------------------------------------------------------

    def floor(self, collection: str) -> Decimal | None:
        if self.market is None:
            return None
        try:
            return self.market.floor(collection)
        except Exception:  # noqa: BLE001 — площадка легла, решение примет цена заявки
            return None

    def _price(self, intent: OrderIntent) -> tuple[Decimal | None, Decimal | None, str]:
        collection, _ = split_instrument(intent.instrument)
        floor = self.floor(collection)
        price = intent.price if intent.price is not None else floor
        if price is None or price <= 0:
            return None, floor, f"{self.venue}: цены нет — ни заявки, ни флора"
        if intent.side == "buy" and floor is not None and floor > 0:
            premium = (price - floor) / floor * HUNDRED
            cap = self.config.secondary.max_floor_premium_pct
            if premium > cap:
                return (
                    None,
                    floor,
                    f"{self.venue}: цена {price} выше флора {floor} на {premium:.1f}% "
                    f"при потолке {cap}%",
                )
        return price, floor, ""

    # -- ордера --------------------------------------------------------------------------

    def place(self, intent: OrderIntent, mode: ModeLiteral) -> Order:
        if mode != self.mode:
            raise NftError(f"{self.venue}: исполнитель в режиме {self.mode}, ордер в режиме {mode}")
        if mode == "live" and not self.private_key:
            raise NotConnected(f"{self.venue}: нет ключа {self.key_env or 'кошелька'}")
        now = datetime.now(UTC)
        order = Order(
            id=f"{self.venue}-{mode}-{next(self._ids)}",
            signal_id=intent.signal_id,
            venue=self.venue,
            client_order_id=intent.client_order_id,
            mode=mode,
            state=OrderState.NEW,
            instrument=intent.instrument,
            side=intent.side,
            qty=intent.qty,
            price=intent.price,
            created_at=now,
        )
        price, floor, refusal = self._price(intent)
        if refusal:
            return self._store(
                order.model_copy(update={"state": OrderState.REJECTED, "reason": refusal})
            )
        assert price is not None
        if mode == "live":
            return self._send(order, intent, price)
        return self._fill(order, intent, price, floor, Costs())

    def _send(self, order: Order, intent: OrderIntent, price: Decimal) -> Order:
        """Живая сделка: покупка листинга или выставление листинга через клиент площадки."""
        if self.client is None:
            raise TradingUnavailable(f"{self.venue}: клиент площадки не подключён")
        self.quota.use(self.venue, 1)
        collection, token_id = split_instrument(intent.instrument)
        result = self.client.trade(
            market=self.venue,
            collection=collection,
            token_id=token_id,
            side=intent.side,
            qty=intent.qty,
            price=price,
            client_order_id=intent.client_order_id,
        )
        if not getattr(result, "ok", False):
            return self._store(
                order.model_copy(
                    update={
                        "state": OrderState.REJECTED,
                        "reason": f"{self.venue}: {getattr(result, 'reason', 'сделка не прошла')}",
                    }
                )
            )
        actual = getattr(result, "price", None) or price
        gas = getattr(result, "gas_usd", Decimal(0)) or Decimal(0)
        return self._fill(order, intent, actual, None, Costs(gas=gas))

    def _fill(
        self,
        order: Order,
        intent: OrderIntent,
        price: Decimal,
        floor: Decimal | None,
        actual: Costs,
    ) -> Order:
        now = datetime.now(UTC)
        estimate = self._estimate(intent, price)
        if actual.gas > 0:  # факт бьёт оценку
            estimate = estimate.model_copy(update={"gas": actual.gas})
        fill = PaperFill(
            id=f"{order.id}-f1",
            order_id=order.id,
            price=price,
            qty=intent.qty,
            fee=estimate.fee,
            fee_asset=self.quote_asset,
            ts=now,
            ref_price=floor if floor is not None else price,
        )
        self._fills.append(fill)
        self._absorb(intent, fill, estimate)
        self._costs[order.id] = estimate
        return self._store(
            order.model_copy(update={"state": OrderState.FILLED, "filled_qty": intent.qty})
        )

    def _estimate(self, intent: OrderIntent, price: Decimal) -> Costs:
        return nft_costs(
            price * intent.qty, market=self.venue, side=intent.side, config=self.config
        )

    def _store(self, order: Order) -> Order:
        self._orders[order.id] = order
        return order

    def _absorb(self, intent: OrderIntent, fill: Fill, costs: Costs) -> None:
        notional = fill.price * fill.qty
        outflow = costs.fee + costs.royalty + costs.gas + costs.priority_fee
        self._cash += (-notional if intent.side == "buy" else notional) - outflow
        pos = self._positions.get(intent.instrument)
        signed = fill.qty if intent.side == "buy" else -fill.qty
        if pos is None:
            if signed != 0:
                self._positions[intent.instrument] = Position(
                    instrument=intent.instrument,
                    side="buy" if signed > 0 else "sell",
                    qty=abs(signed),
                    entry_price=fill.price,
                )
            return
        current = pos.qty if pos.side == "buy" else -pos.qty
        total = current + signed
        if total == 0:
            self._positions.pop(intent.instrument, None)
            return
        entry = pos.entry_price
        if (current >= 0) == (signed >= 0):
            entry = (abs(current) * pos.entry_price + abs(signed) * fill.price) / abs(total)
        self._positions[intent.instrument] = pos.model_copy(
            update={"qty": abs(total), "side": "buy" if total > 0 else "sell", "entry_price": entry}
        )

    def cancel(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise NftError(f"{self.venue}: ордер {order_id} неизвестен")
        if order.state == OrderState.FILLED:
            raise NftError(f"{self.venue}: сделка {order_id} исполнена — отмена невозможна")
        return self._store(order.model_copy(update={"state": OrderState.CANCELLED}))

    def positions(self) -> Sequence[Position]:
        return list(self._positions.values())

    def fills(self, since: datetime) -> Sequence[Fill]:
        return [f for f in self._fills if f.ts >= since]

    def balance(self) -> Sequence[Balance]:
        now = datetime.now(UTC)
        return [Balance(asset=self.quote_asset, total=self._cash, free=self._cash, as_of=now)]

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.state in (OrderState.NEW, OrderState.OPEN)]

    def costs_of(self, order_id: str) -> Costs:
        return self._costs.get(order_id, Costs())

    # -- листинг на продажу (История 78) --------------------------------------------------

    def list_for_sale(self, sell_order, *, signal_id: str = "", strategy_id: str = "") -> Order:
        """Выставить листинг по плану лестницы — тот же путь, что у обычной продажи."""
        from lab.feeds.nft.types import instrument_of

        intent = OrderIntent(
            strategy_id=strategy_id,
            venue=self.venue,
            instrument=instrument_of(sell_order.collection, sell_order.token_id),
            side="sell",
            qty=sell_order.qty,
            price=sell_order.price,
            order_type="limit",
            mode=self.mode,
            signal_id=signal_id or "nft-ladder",
            client_order_id=f"nft-{sell_order.kind}-{sell_order.collection}-{sell_order.price}",
        )
        return self.place(intent, self.mode)

    def health(self) -> Health:
        now = datetime.now(UTC)
        if self.market is None:
            return Health(
                status="degraded",
                detail=f"{self.venue}: площадка не подключена — бумага по цене заявки",
                checked_at=now,
            )
        return self.market.health()


class MagicEdenExecutor(NftExecutor):
    venue = "magiceden"
    chain = "solana"
    quote_asset = "SOL"
    key_env = "SOLANA_HOT_WALLET_KEY"


class OpenSeaExecutor(NftExecutor):
    venue = "opensea"
    chain = "ethereum"
    quote_asset = "ETH"
    key_env = "EVM_HOT_WALLET_KEY"


class ZoraExecutor(NftExecutor):
    venue = "zora"
    chain = "base"
    quote_asset = "ETH"
    key_env = "EVM_HOT_WALLET_KEY"


class TensorExecutor(NftExecutor):
    venue = "tensor"
    chain = "solana"
    quote_asset = "SOL"
    key_env = "SOLANA_HOT_WALLET_KEY"


EXECUTORS: dict[str, type[NftExecutor]] = {
    "magiceden": MagicEdenExecutor,
    "opensea": OpenSeaExecutor,
    "zora": ZoraExecutor,
    "tensor": TensorExecutor,
}


def make_executor(
    market: str,
    source: NftMarketBase | None = None,
    *,
    mode: ModeLiteral = "paper",
    quota: QuotaSink | None = None,
    **kw,
) -> NftExecutor:
    """Исполнитель площадки. Blur и Alchemy — только чтение, исполнителя у них нет."""
    cls = EXECUTORS.get(market)
    if cls is None:
        raise NftError(
            f"{market}: торгового исполнителя нет — площадка либо неизвестна, либо read-only"
        )
    return cls(source, mode=mode, quota=quota, **kw)
