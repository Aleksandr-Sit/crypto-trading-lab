"""Исполнитель Robinhood Crypto — контракт `Executor`, paper и live одним классом (История 85).

Крипта исполняется (когда API открыт для аккаунта), акции — никогда: по ним ветка `rh` отдаёт
только сигналы (`asset_class: stock`, ступень `signal` навсегда), исполняет их оператор руками.
Поэтому ордер по акции здесь — не «не поддерживается», а `SignalOnly`: ошибка вызывающего.

Недоступность API для аккаунта или региона (403/401) — штатный исход: `check_trading_access`
переводит ветку `rh` в режим сигналов (`executors.access`) и это видно в `/status` (История 86).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count
from typing import Any

from lab.contracts import (
    Balance,
    Book,
    Fill,
    Health,
    KeyRights,
    ModeLiteral,
    Order,
    OrderIntent,
    OrderState,
    Position,
    Signal,
)
from lab.core.costs import CostModel, default_model
from lab.executors.access import BranchMode, set_branch_mode
from lab.feeds import NullQuota, QuotaSink
from lab.feeds.robinhood import ORDERS_PATH, RobinhoodError, RobinhoodFeed

VENUE = "robinhood"
BRANCH = "rh"
QUOTE_ASSET = "USD"
STOCK_VENUE = "stocks"


class SignalOnly(RobinhoodError):
    """Акции исполнению не подлежат: только сигнал оператору (История 85, G01)."""


class NotConnected(RobinhoodError):
    """Нет ключей: площадка доступна только на чтение."""


@dataclass(frozen=True)
class TradingAccess:
    available: bool
    reason: str
    checked_at: datetime


class PaperFill(Fill):
    ref_price: Decimal


def is_crypto_pair(instrument: str) -> bool:
    """Крипто-пара — `BTC-USD` (или `BTC/USD` у общих контрактных вызовов); акция — просто тикер."""
    return "-" in instrument or "/" in instrument


class RobinhoodExecutor:
    venue = VENUE
    branch = BRANCH

    def __init__(
        self,
        feed: RobinhoodFeed | None = None,
        *,
        mode: ModeLiteral = "paper",
        costs: CostModel | None = None,
        quota: QuotaSink | None = None,
        paper_balance: Decimal = Decimal(10000),
    ) -> None:
        self.feed = feed
        self.mode: ModeLiteral = mode
        self.costs = costs or default_model()
        self.quota = quota or NullQuota()
        self._ids = count(1)
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._positions: dict[str, Position] = {}
        self._cash = paper_balance

    # -- ключи и доступность -------------------------------------------------------------

    def rights(self) -> KeyRights:
        """У ключа Robinhood Crypto нет права вывода средств — только торговля и чтение."""
        return KeyRights(trade=bool(self.feed and self.feed.has_keys()), withdraw=False)

    def trading_access(self) -> TradingAccess:
        now = datetime.now(UTC)
        signals = "ветка rh — режим сигналов, исполняет оператор"
        if self.feed is None or not self.feed.has_keys():
            return TradingAccess(False, f"нет ключей Robinhood: {signals}", now)
        health = self.feed.health()
        if health.status != "ok":
            return TradingAccess(False, f"{health.detail}: {signals}", now)
        return TradingAccess(True, "Robinhood Crypto доступен", now)

    # -- ордера ----------------------------------------------------------------------------

    def place(self, intent: OrderIntent, mode: ModeLiteral) -> Order:
        if not is_crypto_pair(intent.instrument):
            raise SignalOnly(
                f"robinhood: {intent.instrument} — акция, исполнения нет: только сигнал оператору"
            )
        if mode != self.mode:
            raise RobinhoodError(
                f"robinhood: исполнитель в режиме {self.mode}, ордер в режиме {mode}"
            )
        return self._place_paper(intent) if mode == "paper" else self._place_live(intent)

    def _book(self, instrument: str) -> Book | None:
        if self.feed is None:
            return None
        try:
            return self.feed.book(instrument)
        except RobinhoodError:
            return None

    def _place_paper(self, intent: OrderIntent) -> Order:
        now = datetime.now(UTC)
        book = self._book(intent.instrument)
        levels = (book.asks if intent.side == "buy" else book.bids) if book else []
        price = levels[0].price if levels else intent.price
        order = Order(
            id=f"rh-paper-{next(self._ids)}",
            signal_id=intent.signal_id,
            venue=VENUE,
            client_order_id=intent.client_order_id,
            mode="paper",
            state=OrderState.OPEN,
            instrument=intent.instrument,
            side=intent.side,
            qty=intent.qty,
            price=intent.price,
            created_at=now,
        )
        if price is None:
            self._orders[order.id] = order
            return order
        if intent.price is not None:
            crosses = price <= intent.price if intent.side == "buy" else price >= intent.price
            if not crosses:
                self._orders[order.id] = order
                return order
        # глубины Robinhood не отдаёт (только лучшие цены со спредом), поэтому издержки —
        # по тарифу площадки, а не по стакану: пустые уровни дали бы ложный VWAP
        costs = self.costs.estimate(VENUE, intent)
        fill = PaperFill(
            id=f"{order.id}-f1",
            order_id=order.id,
            price=price,
            qty=intent.qty,
            fee=costs.fee,
            fee_asset=QUOTE_ASSET,
            ts=now,
            ref_price=price,
        )
        self._fills.append(fill)
        self._absorb(intent, fill)
        order = order.model_copy(update={"state": OrderState.FILLED, "filled_qty": intent.qty})
        self._orders[order.id] = order
        return order

    def _absorb(self, intent: OrderIntent, fill: Fill) -> None:
        notional = fill.price * fill.qty
        self._cash += (-notional if intent.side == "buy" else notional) - fill.fee
        pos = self._positions.get(intent.instrument)
        signed = fill.qty if intent.side == "buy" else -fill.qty
        current = (pos.qty if pos.side == "buy" else -pos.qty) if pos else Decimal(0)
        total = current + signed
        if total == 0:
            self._positions.pop(intent.instrument, None)
            return
        entry = fill.price
        if pos is not None and (current >= 0) == (signed >= 0):
            entry = (abs(current) * pos.entry_price + abs(signed) * fill.price) / abs(total)
        self._positions[intent.instrument] = Position(
            instrument=intent.instrument,
            side="buy" if total > 0 else "sell",
            qty=abs(total),
            entry_price=entry,
        )

    def _place_live(self, intent: OrderIntent) -> Order:
        if self.feed is None or not self.feed.has_keys():
            raise NotConnected(
                "robinhood: нет ключей ROBINHOOD_API_KEY/ROBINHOOD_PRIVATE_KEY — только чтение"
            )
        payload: dict[str, Any] = {
            "client_order_id": intent.client_order_id,
            "side": intent.side,
            "symbol": intent.instrument,
            "type": intent.order_type,
        }
        config = {"asset_quantity": str(intent.qty)}
        if intent.order_type == "limit":
            config["limit_price"] = str(intent.price)
            config["time_in_force"] = "gtc"
        payload[f"{intent.order_type}_order_config"] = config
        raw = self.feed.call("POST", ORDERS_PATH, payload=payload)
        return self._parse_order(raw, intent)

    def _parse_order(self, raw: dict[str, Any], intent: OrderIntent) -> Order:
        states = {
            "filled": OrderState.FILLED,
            "open": OrderState.OPEN,
            "new": OrderState.NEW,
            "partially_filled": OrderState.PARTIAL,
            "canceled": OrderState.CANCELLED,
            "rejected": OrderState.REJECTED,
        }
        order = Order(
            id=str(raw.get("id") or intent.client_order_id),
            signal_id=intent.signal_id,
            venue=VENUE,
            client_order_id=str(raw.get("client_order_id") or intent.client_order_id),
            mode="live",
            state=states.get(str(raw.get("state", "open")), OrderState.OPEN),
            instrument=intent.instrument,
            side=intent.side,
            qty=intent.qty,
            price=intent.price,
            filled_qty=Decimal(str(raw.get("filled_asset_quantity", "0") or "0")),
            created_at=datetime.now(UTC),
        )
        self._orders[order.id] = order
        return order

    def cancel(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise RobinhoodError(f"robinhood: ордер {order_id} неизвестен")
        if order.mode == "live" and self.feed is not None:
            self.feed.call("POST", f"{ORDERS_PATH}{order_id}/cancel/")
        cancelled = order.model_copy(update={"state": OrderState.CANCELLED})
        self._orders[order_id] = cancelled
        return cancelled

    # -- состояние ---------------------------------------------------------------------------

    def positions(self) -> Sequence[Position]:
        if self.mode == "paper":
            return list(self._positions.values())
        if self.feed is None or not self.feed.has_keys():
            return []
        out: list[Position] = []
        for row in self.feed.holdings():
            qty = Decimal(str(row.get("total_quantity", "0") or "0"))
            if qty <= 0:
                continue
            out.append(
                Position(
                    instrument=f"{row.get('asset_code', '')}-USD",
                    side="buy",
                    qty=qty,
                    entry_price=Decimal(0),
                )
            )
        return out

    def fills(self, since: datetime) -> Sequence[Fill]:
        if self.mode == "paper":
            return [f for f in self._fills if f.ts >= since]
        if self.feed is None or not self.feed.has_keys():
            return []
        out: list[Fill] = []
        for row in self.feed.orders({"state": "filled"}):
            qty = Decimal(str(row.get("filled_asset_quantity", "0") or "0"))
            if qty <= 0:
                continue
            out.append(
                Fill(
                    id=str(row.get("id", "")),
                    order_id=str(row.get("id", "")),
                    price=Decimal(str(row.get("average_price", "0") or "0")),
                    qty=qty,
                    fee=Decimal(0),
                    fee_asset=QUOTE_ASSET,
                    ts=datetime.now(UTC),
                )
            )
        return out

    def balance(self) -> Sequence[Balance]:
        now = datetime.now(UTC)
        if self.mode == "paper":
            return [Balance(asset=QUOTE_ASSET, total=self._cash, free=self._cash, as_of=now)]
        if self.feed is None or not self.feed.has_keys():
            return []
        account = self.feed.account()
        power = Decimal(str(account.get("buying_power", "0") or "0"))
        return [
            Balance(
                asset=str(account.get("buying_power_currency", QUOTE_ASSET)),
                total=power,
                free=power,
                as_of=now,
            )
        ]

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.state in (OrderState.NEW, OrderState.OPEN)]

    def health(self) -> Health:
        if self.feed is None:
            return Health(
                status="degraded",
                detail="robinhood: фида нет — бумага по цене заявки",
                checked_at=datetime.now(UTC),
            )
        return self.feed.health()


def stock_signal_card(
    signal: Signal, *, signal_id: str, rung: str = "signal"
) -> tuple[str, dict[str, Any]]:
    """Путь доставки сигнала по акции: карточка `signal` для бота (Истории 85, 85a).

    Стратегии акций живут в `strategies.stocks` (таск 07) — здесь только доставка:
    исполнения нет, оператор отмечает исход кнопкой.
    """
    return "signal", {
        "signal_id": signal_id,
        "strategy_id": signal.strategy_id,
        "rung": rung,
        "instrument": signal.instrument,
        "side": signal.side,
        "size": signal.size,
        "price_ref": signal.price_ref,
        "venue": STOCK_VENUE,
        "ttl_s": signal.ttl_s,
    }


def check_trading_access(
    executor: RobinhoodExecutor, *, session: Any = None, branch: str = BRANCH
) -> BranchMode:
    """Проверка при старте: API закрыт → ветка `rh` в режиме сигналов с пометкой."""
    access = executor.trading_access()
    if session is not None:
        return set_branch_mode(
            session, branch, read_only=not access.available, reason=access.reason
        )
    return BranchMode(
        branch=branch,
        read_only=not access.available,
        reason=access.reason,
        checked_at=access.checked_at,
    )


__all__ = [
    "BRANCH",
    "QUOTE_ASSET",
    "STOCK_VENUE",
    "VENUE",
    "NotConnected",
    "PaperFill",
    "RobinhoodExecutor",
    "SignalOnly",
    "TradingAccess",
    "check_trading_access",
    "is_crypto_pair",
    "stock_signal_card",
]
