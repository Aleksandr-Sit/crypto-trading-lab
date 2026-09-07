"""Исполнитель Polymarket (CLOB) — контракт `Executor`, режимы paper и live одним классом.

Режим задаётся экземпляру, как у `executors.cex`: `positions/fills/balance` бумажного
экземпляра — расчёт в памяти, живого — только с площадки; ордер с чужим режимом отклоняется.
Бумага исполняется по живому стакану CLOB и модели издержек `core.costs` — тот же код, что
считает реальность, иначе разница «бумага vs живьё» была бы артефактом кода.

Живая торговля требует подписи L1/L2 и кошелька: клиент подписи (`py-clob-client`) грузится
лениво и только в режиме live — его тяжёлое дерево (web3/eth-account) не нужно ни фиду, ни
бумаге, а ветка `prediction` обязана работать и там, где торговля закрыта (История 84).
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
)
from lab.core.costs import CostModel, default_model
from lab.executors.access import BranchMode, set_branch_mode
from lab.feeds import NullQuota, QuotaSink
from lab.feeds.polymarket import PolymarketError, PolymarketFeed

VENUE = "polymarket"
BRANCH = "prediction"
QUOTE_ASSET = "USDC"
GEO_MARKERS = ("403", "forbidden", "restricted", "blocked", "not available")


class NotConnected(PolymarketError):
    """Нет ключа кошелька: площадка доступна только на чтение (Истории 84, 53)."""


class TradingUnavailable(PolymarketError):
    """Торговля закрыта для этого IP/аккаунта — ветка живёт в режиме «только замер»."""


@dataclass(frozen=True)
class TradingAccess:
    available: bool
    reason: str
    checked_at: datetime


class PaperFill(Fill):
    """Бумажный филл: плюс мид стакана на момент решения — для `Journal.record_fill(ref_price=)`."""

    ref_price: Decimal


class PolymarketExecutor:
    """`feed=None` — исполнитель без источника котировок: бумага исполняется по цене заявки."""

    venue = VENUE
    branch = BRANCH

    def __init__(
        self,
        feed: PolymarketFeed | None = None,
        *,
        mode: ModeLiteral = "paper",
        costs: CostModel | None = None,
        quota: QuotaSink | None = None,
        paper_balance: Decimal = Decimal(10000),
        private_key: str | None = None,
        client: Any = None,
    ) -> None:
        self.feed = feed
        self.mode: ModeLiteral = mode
        self.costs = costs or default_model()
        self.quota = quota or NullQuota()
        self.private_key = private_key
        self._client = client
        self._ids = count(1)
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._positions: dict[str, Position] = {}
        self._cash = paper_balance
        self._start_cash = paper_balance

    # -- ключи и доступность ----------------------------------------------------------

    def rights(self) -> KeyRights:
        """Вывод средств ключом CLOB невозможен by design: кошелёк подписывает только ордера."""
        return KeyRights(trade=bool(self.private_key or self._client), withdraw=False)

    def trading_access(self) -> TradingAccess:
        now = datetime.now(UTC)
        if self.feed is not None:
            try:
                probe = self.feed.health()
            except PolymarketError as err:
                return TradingAccess(False, f"CLOB недоступен: {err}", now)
            detail = probe.detail.lower()
            if any(marker in detail for marker in GEO_MARKERS):
                return TradingAccess(False, f"торговля закрыта для этого IP: {probe.detail}", now)
            if probe.status == "down":
                return TradingAccess(False, f"CLOB недоступен: {probe.detail}", now)
        if not (self.private_key or self._client):
            return TradingAccess(
                False,
                "нет ключа POLYMARKET_PRIVATE_KEY — ветка prediction в режиме «только замер»",
                now,
            )
        try:
            self._live_client()
        except PolymarketError as err:
            return TradingAccess(False, str(err), now)
        return TradingAccess(True, "торговля доступна", now)

    def _live_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.private_key:
            raise NotConnected("polymarket: нет ключа POLYMARKET_PRIVATE_KEY — только чтение")
        try:  # тяжёлое дерево подписи грузится только здесь
            from py_clob_client.client import ClobClient  # type: ignore[import-not-found]
        except ImportError as err:  # pragma: no cover — зависит от окружения
            raise TradingUnavailable(
                "polymarket: для live нужен py-clob-client (подпись L1/L2); ветка идёт "
                "в режиме «только замер»"
            ) from err
        base = self.feed.clob_base if self.feed is not None else "https://clob.polymarket.com"
        self._client = ClobClient(base, key=self.private_key, chain_id=137)
        return self._client

    # -- ордера -----------------------------------------------------------------------

    def place(self, intent: OrderIntent, mode: ModeLiteral) -> Order:
        if mode != self.mode:
            raise PolymarketError(
                f"polymarket: исполнитель в режиме {self.mode}, ордер в режиме {mode}"
            )
        return self._place_paper(intent) if mode == "paper" else self._place_live(intent)

    def _book(self, instrument: str) -> Book | None:
        if self.feed is None:
            return None
        try:
            return self.feed.book(instrument)
        except PolymarketError:
            return None

    def _fill_price(self, intent: OrderIntent, book: Book | None) -> Decimal | None:
        levels = (book.asks if intent.side == "buy" else book.bids) if book else []
        if not levels:
            return intent.price
        left, cost = intent.qty, Decimal(0)
        for level in levels:
            take = min(left, level.qty)
            cost += take * level.price
            left -= take
            if left <= 0:
                break
        if left > 0:  # глубины не хватило — остаток по последнему уровню
            cost += left * levels[-1].price
        price = cost / intent.qty if intent.qty else levels[0].price
        if intent.price is not None:
            crosses = price <= intent.price if intent.side == "buy" else price >= intent.price
            if not crosses:
                return None  # лимитка не пересекает рынок — остаётся в стакане
        return price

    def _place_paper(self, intent: OrderIntent) -> Order:
        now = datetime.now(UTC)
        book = self._book(intent.instrument)
        price = self._fill_price(intent, book)
        order = Order(
            id=f"pm-paper-{next(self._ids)}",
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
        costs = self.costs.estimate(VENUE, intent, book)
        fill = PaperFill(
            id=f"{order.id}-f1",
            order_id=order.id,
            price=price,
            qty=intent.qty,
            fee=costs.fee,
            fee_asset=QUOTE_ASSET,
            ts=now,
            ref_price=self._mid(book) or price,
        )
        self._fills.append(fill)
        self._absorb(intent, fill)
        order = order.model_copy(update={"state": OrderState.FILLED, "filled_qty": intent.qty})
        self._orders[order.id] = order
        return order

    @staticmethod
    def _mid(book: Book | None) -> Decimal | None:
        if book and book.bids and book.asks:
            return (book.bids[0].price + book.asks[0].price) / 2
        return None

    def _absorb(self, intent: OrderIntent, fill: Fill) -> None:
        notional = fill.price * fill.qty
        self._cash += (-notional if intent.side == "buy" else notional) - fill.fee
        pos = self._positions.get(intent.instrument)
        signed = fill.qty if intent.side == "buy" else -fill.qty
        if pos is None:
            if signed == 0:
                return
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
        if (current >= 0) == (signed >= 0):  # доливка — средняя цена входа
            entry = (abs(current) * pos.entry_price + abs(signed) * fill.price) / abs(total)
        self._positions[intent.instrument] = pos.model_copy(
            update={"qty": abs(total), "side": "buy" if total > 0 else "sell", "entry_price": entry}
        )

    def _place_live(self, intent: OrderIntent) -> Order:
        client = self._live_client()
        self.quota.use(VENUE, 1)
        raw = client.create_and_post_order(
            {
                "token_id": intent.instrument,
                "price": float(intent.price) if intent.price is not None else None,
                "size": float(intent.qty),
                "side": intent.side.upper(),
            }
        )
        order = Order(
            id=str(raw.get("orderID") or raw.get("id") or intent.client_order_id),
            signal_id=intent.signal_id,
            venue=VENUE,
            client_order_id=intent.client_order_id,
            mode="live",
            state=OrderState.OPEN if raw.get("success", True) else OrderState.REJECTED,
            instrument=intent.instrument,
            side=intent.side,
            qty=intent.qty,
            price=intent.price,
            created_at=datetime.now(UTC),
            reason=str(raw.get("errorMsg", "")),
        )
        self._orders[order.id] = order
        return order

    def cancel(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise PolymarketError(f"polymarket: ордер {order_id} неизвестен")
        if order.mode == "live":
            self._live_client().cancel(order_id)
        cancelled = order.model_copy(update={"state": OrderState.CANCELLED})
        self._orders[order_id] = cancelled
        return cancelled

    # -- состояние --------------------------------------------------------------------

    def positions(self) -> Sequence[Position]:
        if self.mode == "paper":
            return list(self._positions.values())
        wallet = self.wallet()
        if not wallet or self.feed is None:
            return []
        return [
            Position(
                instrument=p.token_id,
                side="buy",
                qty=p.size,
                entry_price=p.avg_price,
                unrealized_pnl=(p.current_price - p.avg_price) * p.size,
            )
            for p in self.feed.positions(wallet)
        ]

    def fills(self, since: datetime) -> Sequence[Fill]:
        if self.mode == "paper":
            return [f for f in self._fills if f.ts >= since]
        wallet = self.wallet()
        if not wallet or self.feed is None:
            return []
        out: list[Fill] = []
        for row in self.feed.wallet_trades(wallet):
            ts = datetime.fromtimestamp(float(row.get("timestamp", 0)), tz=UTC)
            if ts < since:
                continue
            out.append(
                Fill(
                    id=str(row.get("transactionHash", "")),
                    order_id=str(row.get("orderHash") or row.get("transactionHash", "")),
                    price=Decimal(str(row.get("price", "0"))),
                    qty=Decimal(str(row.get("size", "0"))),
                    fee=Decimal(str(row.get("fee", "0"))),
                    fee_asset=QUOTE_ASSET,
                    ts=ts,
                )
            )
        return out

    def balance(self) -> Sequence[Balance]:
        now = datetime.now(UTC)
        if self.mode == "paper":
            return [Balance(asset=QUOTE_ASSET, total=self._cash, free=self._cash, as_of=now)]
        if not (self.private_key or self._client):
            return []
        raw = self._live_client().get_balance_allowance()
        total = Decimal(str(raw.get("balance", "0"))) / Decimal(10**6)
        return [Balance(asset=QUOTE_ASSET, total=total, free=total, as_of=now)]

    def wallet(self) -> str:
        client = self._client
        address = getattr(client, "get_address", None)
        if callable(address):
            return str(address())
        return str(getattr(client, "address", "") or "")

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.state in (OrderState.NEW, OrderState.OPEN)]

    def health(self) -> Health:
        now = datetime.now(UTC)
        if self.feed is None:
            return Health(
                status="degraded",
                detail="polymarket: фида нет — бумага по цене заявки",
                checked_at=now,
            )
        return self.feed.health()


def check_trading_access(
    executor: PolymarketExecutor,
    *,
    session: Any = None,
    branch: str = BRANCH,
) -> BranchMode:
    """Проверка при старте: недоступна торговля → ветка переходит в «только замер»."""
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


def make_executor(
    mode: ModeLiteral = "paper",
    *,
    feed: PolymarketFeed | None = None,
    quota: QuotaSink | None = None,
    private_key: str | None = None,
) -> PolymarketExecutor:
    return PolymarketExecutor(feed, mode=mode, quota=quota, private_key=private_key)


__all__ = [
    "BRANCH",
    "QUOTE_ASSET",
    "VENUE",
    "NotConnected",
    "PaperFill",
    "PolymarketExecutor",
    "TradingAccess",
    "TradingUnavailable",
    "check_trading_access",
    "make_executor",
]
