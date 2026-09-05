"""FakeExecutor — исполнитель без площадки для тестов и для режима paper без сети.

Правила бумажного исполнения:
- market → заполняется по mark-цене;
- limit buy с price >= mark и limit sell с price <= mark → заполняется по лимитной цене;
- прочие лимитки остаются open до cancel().
Комиссия — плоский taker fee (0.1%) в котируемом активе.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count

from lab.contracts import (
    Balance,
    Fill,
    Health,
    KeyRights,
    ModeLiteral,
    Order,
    OrderIntent,
    OrderState,
    Position,
)

TAKER_FEE = Decimal("0.001")


class FakeExecutor:
    venue = "fake"

    def __init__(
        self,
        mark_price: Decimal = Decimal("100"),
        start_balance: Decimal = Decimal("10000"),
        quote_asset: str = "USDT",
    ) -> None:
        self.mark_price = mark_price
        self.quote_asset = quote_asset
        self._balance = start_balance
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._positions: dict[str, Position] = {}
        self._ids = count(1)

    # -- контракт -------------------------------------------------------------

    def rights(self) -> KeyRights:
        return KeyRights(trade=True, withdraw=False)

    def place(self, intent: OrderIntent, mode: ModeLiteral) -> Order:
        if mode == "live":
            raise RuntimeError("FakeExecutor не умеет live: площадки нет")
        order_id = f"fake-{next(self._ids)}"
        now = datetime.now(UTC)
        fill_price = self._fill_price(intent)
        state = OrderState.FILLED if fill_price is not None else OrderState.OPEN
        order = Order(
            id=order_id,
            signal_id=intent.signal_id,
            venue=self.venue,
            client_order_id=intent.client_order_id,
            mode=mode,
            state=state,
            instrument=intent.instrument,
            side=intent.side,
            qty=intent.qty,
            price=intent.price,
            filled_qty=intent.qty if fill_price is not None else Decimal(0),
            created_at=now,
        )
        self._orders[order_id] = order
        if fill_price is not None:
            self._apply_fill(order, fill_price, now)
        return order

    def cancel(self, order_id: str) -> Order:
        order = self._orders[order_id]
        if order.state in (OrderState.OPEN, OrderState.PARTIAL, OrderState.NEW):
            order = order.model_copy(update={"state": OrderState.CANCELLED})
            self._orders[order_id] = order
        return order

    def positions(self) -> Sequence[Position]:
        return list(self._positions.values())

    def fills(self, since: datetime) -> Sequence[Fill]:
        return [f for f in self._fills if f.ts >= since]

    def balance(self) -> Sequence[Balance]:
        return [
            Balance(
                asset=self.quote_asset,
                total=self._balance,
                free=self._balance,
                as_of=datetime.now(UTC),
            )
        ]

    def health(self) -> Health:
        return Health(status="ok", detail="fake", checked_at=datetime.now(UTC))

    # -- внутреннее -----------------------------------------------------------

    def _fill_price(self, intent: OrderIntent) -> Decimal | None:
        if intent.order_type == "market" or intent.price is None:
            return self.mark_price
        crosses = (intent.side == "buy" and intent.price >= self.mark_price) or (
            intent.side == "sell" and intent.price <= self.mark_price
        )
        return intent.price if crosses else None

    def _apply_fill(self, order: Order, price: Decimal, ts: datetime) -> None:
        notional = price * order.qty
        fee = notional * TAKER_FEE
        self._fills.append(
            Fill(
                id=f"{order.id}-f1",
                order_id=order.id,
                price=price,
                qty=order.qty,
                fee=fee,
                fee_asset=self.quote_asset,
                ts=ts,
            )
        )
        signed = order.qty if order.side == "buy" else -order.qty
        self._balance -= signed * price + fee
        current = self._positions.get(order.instrument)
        net = (current.qty if current and current.side == "buy" else Decimal(0)) - (
            current.qty if current and current.side == "sell" else Decimal(0)
        )
        net += signed
        if net == 0:
            self._positions.pop(order.instrument, None)
            return
        self._positions[order.instrument] = Position(
            instrument=order.instrument,
            side="buy" if net > 0 else "sell",
            qty=abs(net),
            entry_price=price,
        )
