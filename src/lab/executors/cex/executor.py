"""CexExecutor — контракт Executor для Bybit/OKX/Binance/Hyperliquid, paper и live одним классом.

paper: живые котировки площадки (через тот же транспорт) + `core.costs.estimate` для комиссии и
проскальзывания; состояние (ордера, филлы, позиции, баланс) — в памяти.
live: ордера на площадку через ccxt с детерминированным `client_order_id` (от `signal_id`);
после таймаута/обрыва ордер ищется на площадке по client id — дублей нет (История 55).
`restore(session)` поднимает активные ордера из таблицы `orders` и сверяет с площадкой (56).
Плечо перпа: не выше максимума площадки (лимит ветки проверяет `core.risk.check` раньше);
фандинг и цена ликвидации — из ответа площадки (`perp_info`, `funding_payments`).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count
from typing import Any

import ccxt
from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.contracts import (
    Balance,
    BookLevel,
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
from lab.db.models import OrderRow
from lab.feeds import QuotaSink
from lab.feeds.cex.feed import CexFeed, FundingRate
from lab.feeds.cex.transport import VENUE_SPECS, Transport

log = logging.getLogger(__name__)

ACTIVE_STATES = (OrderState.NEW, OrderState.OPEN, OrderState.PARTIAL)


class CexError(RuntimeError):
    pass


class NotConnected(CexError):
    """Ключа нет: площадка в режиме «только данные» (История 53)."""


class KeyRejected(CexError):
    """Ключ с правом вывода средств не используется (История 54, решение §13)."""


def client_order_id(signal_id: str, venue: str) -> str:
    """Детерминированный client id от signal_id, допустимый на всех площадках:
    OKX — ≤32 буквенно-цифровых символов; Hyperliquid — cloid 0x + 32 hex."""
    digest = hashlib.sha256(f"{venue}:{signal_id}".encode()).hexdigest()
    if venue == "hyperliquid":
        return "0x" + digest[:32]
    return (
        VENUE_SPECS[venue].client_id_prefix
        + digest[: 32 - len(VENUE_SPECS[venue].client_id_prefix)]
    )


@dataclass(frozen=True)
class PerpInfo:
    instrument: str
    leverage: Decimal
    max_leverage: Decimal
    funding_rate: Decimal
    next_funding_at: datetime | None
    mark_price: Decimal | None
    liquidation_price: Decimal | None


@dataclass(frozen=True)
class FundingPayment:
    id: str
    instrument: str
    amount: Decimal
    ts: datetime


class PaperFill(Fill):
    """Бумажный филл: плюс мид на момент решения — `Journal.record_fill(..., ref_price=)`."""

    ref_price: Decimal


@dataclass(frozen=True)
class ReconcileResult:
    orders: list[Order]
    fills: list[Fill]
    positions: list[Position]


def _dec(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    return Decimal(str(value)) if value is not None else default


def _ts(ms: int | None) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC) if ms else datetime.now(UTC)


def _walk_book(levels: Sequence[BookLevel], qty: Decimal) -> Decimal:
    """VWAP исполнения рыночного ордера по стакану; остаток сверх глубины — по последнему уровню."""
    left, cost = qty, Decimal(0)
    for level in levels:
        take = min(left, level.qty)
        cost += take * level.price
        left -= take
        if left <= 0:
            break
    if left > 0:
        cost += left * levels[-1].price
    return cost / qty


def _state(raw: dict[str, Any]) -> OrderState:
    status = (raw.get("status") or "open").lower()
    filled, amount = _dec(raw.get("filled")), _dec(raw.get("amount"))
    if status == "open":
        return OrderState.PARTIAL if filled > 0 else OrderState.OPEN
    if status == "closed":
        return OrderState.FILLED if filled >= amount or amount == 0 else OrderState.CANCELLED
    if status in ("canceled", "cancelled", "expired"):
        return OrderState.CANCELLED
    return OrderState.REJECTED


class CexExecutor:
    venue: str = ""

    def __init__(
        self,
        transport: Transport,
        *,
        mode: ModeLiteral = "paper",
        costs: CostModel | None = None,
        quota: QuotaSink | None = None,
        paper_balance: Decimal = Decimal("10000"),
    ) -> None:
        """`mode` экземпляра задаёт, чьё состояние отдают positions/fills/balance:
        paper — память (расчёт), live — только площадка. `place` с другим режимом — CexError."""
        self.mode: ModeLiteral = mode
        self.venue = transport.id
        self.transport = transport
        self.feed = CexFeed(transport, quota=quota)
        self.costs = costs or default_model()
        self.quote = VENUE_SPECS[self.venue].quote_asset
        self._rights: KeyRights | None = None
        self._orders: dict[str, Order] = {}
        self._by_coid: dict[str, str] = {}
        self._instruments: set[str] = set()
        self._leverage: dict[str, Decimal] = {}
        self._liquidation: dict[str, Decimal | None] = {}
        self._ids = count(1)
        self._paper_balance = paper_balance
        self._paper_fills: list[PaperFill] = []
        self._last_balance: list[Balance] | None = None
        self._paper_positions: dict[str, Position] = {}

    # -- служебное ----------------------------------------------------------------

    def _call(self, name: str, *args: Any, **kw: Any) -> Any:
        return self.feed._call(name, *args, **kw)

    def _is_perp(self, instrument: str) -> bool:
        try:
            return bool(self.transport.market(instrument).get("swap"))
        except Exception:  # noqa: BLE001 — рынки не загружены: судим по символу
            return ":" in instrument

    def max_leverage(self, instrument: str) -> Decimal:
        """Максимум плеча по данным площадки; неизвестен → 1 (плечо >1 отклоняется)."""
        try:
            limit = self.transport.market(instrument)["limits"]["leverage"]["max"]
        except Exception:  # noqa: BLE001 — рынки не загружены или лимита нет
            limit = None
        return _dec(limit, Decimal(1))

    def _remember(self, order: Order) -> Order:
        self._orders[order.id] = order
        self._by_coid[order.client_order_id] = order.id
        self._instruments.add(order.instrument)
        return order

    def _parse_order(
        self, raw: dict[str, Any], *, signal_id: str = "", mode: ModeLiteral = "live"
    ) -> Order:
        known = self._orders.get(str(raw.get("id")))
        return Order(
            id=str(raw["id"]),
            signal_id=signal_id or (known.signal_id if known else ""),
            venue=self.venue,
            client_order_id=str(
                raw.get("clientOrderId") or (known.client_order_id if known else "")
            ),
            mode=mode,
            state=_state(raw),
            instrument=str(raw["symbol"]),
            side=raw["side"],
            qty=_dec(raw.get("amount")),
            price=_dec(raw["price"]) if raw.get("price") is not None else None,
            filled_qty=_dec(raw.get("filled")),
            created_at=_ts(raw.get("timestamp")),
        )

    def _parse_fill(self, raw: dict[str, Any]) -> Fill:
        fee = raw.get("fee") or {}
        return Fill(
            id=str(raw["id"]),
            order_id=str(raw.get("order") or ""),
            price=_dec(raw["price"]),
            qty=_dec(raw["amount"]),
            fee=_dec(fee.get("cost")),
            fee_asset=str(fee.get("currency") or self.quote),
            ts=_ts(raw.get("timestamp")),
        )

    def _find_on_venue(self, instrument: str, coid: str) -> Order | None:
        for name in ("fetch_open_orders", "fetch_closed_orders"):
            try:
                rows = (
                    self._call(name, instrument)
                    if name == "fetch_open_orders"
                    else self._call(name, instrument, None)
                )
            except ccxt.BaseError as err:
                log.warning("%s: %s не удался при поиске %s: %s", self.venue, name, coid, err)
                continue
            for raw in rows:
                if raw.get("clientOrderId") == coid:
                    return self._parse_order(raw)
        return None

    # -- контракт Executor ---------------------------------------------------------

    def rights(self) -> KeyRights:
        if self._rights is None:
            if not self.transport.has_keys:
                self._rights = KeyRights(trade=False, withdraw=False)
            else:
                raw = self._call("key_rights")
                self._rights = KeyRights(
                    trade=bool(raw.get("trade")), withdraw=bool(raw.get("withdraw"))
                )
                if self._rights.withdraw:
                    log.warning(
                        "%s: у ключа есть право вывода средств — торговля отключена", self.venue
                    )
        return self._rights

    def place(self, intent: OrderIntent, mode: ModeLiteral) -> Order:
        if mode != self.mode:
            raise CexError(f"{self.venue}: исполнитель в режиме {self.mode}, ордер в режиме {mode}")
        now = datetime.now(UTC)
        if intent.leverage > 1 or self._is_perp(intent.instrument):
            limit = self.max_leverage(intent.instrument)
            if intent.leverage > limit:
                return self._remember(
                    Order(
                        id=f"rejected-{next(self._ids)}",
                        signal_id=intent.signal_id,
                        venue=self.venue,
                        client_order_id=intent.client_order_id,
                        mode=mode,
                        state=OrderState.REJECTED,
                        instrument=intent.instrument,
                        side=intent.side,
                        qty=intent.qty,
                        price=intent.price,
                        created_at=now,
                        reason=f"плечо {intent.leverage} выше максимума площадки {limit}",
                    )
                )
        if mode == "paper":
            return self._place_paper(intent, now)
        return self._place_live(intent)

    def _place_paper(self, intent: OrderIntent, now: datetime) -> Order:
        book = self.feed.book(intent.instrument)  # живой стакан площадки
        levels = book.asks if intent.side == "buy" else book.bids
        touch = levels[0].price if levels else None
        fill_price: Decimal | None
        if touch is None:
            fill_price = None
        elif intent.order_type == "market" or intent.price is None:
            fill_price = _walk_book(levels, intent.qty)
        else:
            crosses = (intent.side == "buy" and intent.price >= touch) or (
                intent.side == "sell" and intent.price <= touch
            )
            fill_price = intent.price if crosses else None
        order = Order(
            id=f"paper-{self.venue}-{next(self._ids)}",
            signal_id=intent.signal_id,
            venue=self.venue,
            client_order_id=intent.client_order_id,
            mode="paper",
            state=OrderState.FILLED if fill_price is not None else OrderState.OPEN,
            instrument=intent.instrument,
            side=intent.side,
            qty=intent.qty,
            price=intent.price,
            filled_qty=intent.qty if fill_price is not None else Decimal(0),
            created_at=now,
        )
        self._remember(order)
        if fill_price is not None:
            est = self.costs.estimate(self.venue, intent, book)  # та же модель издержек, что у live
            two_sided = book.bids and book.asks
            mid = (book.bids[0].price + book.asks[0].price) / 2 if two_sided else touch
            self._apply_paper_fill(order, fill_price, est.fee, now, ref_price=mid)
        if intent.leverage > 1:
            self._leverage[intent.instrument] = intent.leverage
        return order

    def _apply_paper_fill(
        self, order: Order, price: Decimal, fee: Decimal, ts: datetime, *, ref_price: Decimal
    ) -> None:
        self._paper_fills.append(
            PaperFill(
                id=f"{order.id}-f1",
                order_id=order.id,
                price=price,
                qty=order.qty,
                fee=fee,
                fee_asset=self.quote,
                ts=ts,
                ref_price=ref_price,
            )
        )
        signed = order.qty if order.side == "buy" else -order.qty
        self._paper_balance -= signed * price + fee
        current = self._paper_positions.get(order.instrument)
        net = Decimal(0)
        if current:
            net = current.qty if current.side == "buy" else -current.qty
        net += signed
        if net == 0:
            self._paper_positions.pop(order.instrument, None)
            return
        self._paper_positions[order.instrument] = Position(
            instrument=order.instrument,
            side="buy" if net > 0 else "sell",
            qty=abs(net),
            entry_price=price,
            leverage=self._leverage.get(order.instrument, Decimal(1)),
        )

    def _place_live(self, intent: OrderIntent) -> Order:
        rights = self.rights()
        if not self.transport.has_keys or not rights.trade:
            raise NotConnected(f"{self.venue}: ключа нет — площадка в режиме «только данные»")
        if rights.withdraw:
            raise KeyRejected(f"{self.venue}: ключ с правом вывода средств отклонён")
        coid = intent.client_order_id
        if coid in self._by_coid:
            return self._orders[self._by_coid[coid]]
        known = self._find_on_venue(intent.instrument, coid)
        if known is not None:
            return self._remember(known.model_copy(update={"signal_id": intent.signal_id}))
        if intent.leverage > 1 and self._leverage.get(intent.instrument) != intent.leverage:
            self._call("set_leverage", int(intent.leverage), intent.instrument)
            self._leverage[intent.instrument] = intent.leverage
        params: dict[str, Any] = {"clientOrderId": coid}
        if intent.reduce_only and self._is_perp(intent.instrument):
            # На споте флага у биржи нет, а ccxt не срезает его и шлёт как есть. Закрытие
            # на споте — продажа не больше лотов стратегии: это сверяет `place_signal`.
            params["reduceOnly"] = True
        try:
            raw = self._call(
                "create_order",
                intent.instrument,
                intent.order_type,
                intent.side,
                float(intent.qty),
                float(intent.price) if intent.price is not None else None,
                params,
            )
        except ccxt.NetworkError as err:  # RequestTimeout — тоже NetworkError
            log.warning("%s: обрыв при отправке %s, ищу на площадке: %s", self.venue, coid, err)
            known = self._find_on_venue(intent.instrument, coid)
            if known is None:
                raise
            raw = None
            order = known.model_copy(update={"signal_id": intent.signal_id})
        if raw is not None:
            order = self._parse_order(raw, signal_id=intent.signal_id)
        return self._remember(order)

    def cancel(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is not None and order.mode == "paper":
            if order.state in ACTIVE_STATES:
                order = order.model_copy(update={"state": OrderState.CANCELLED})
            return self._remember(order)
        instrument = order.instrument if order else None
        raw = self._call("cancel_order", order_id, instrument)
        return self._remember(self._parse_order(raw))

    def positions(self) -> Sequence[Position]:
        if self.mode == "paper":
            return list(self._paper_positions.values())
        out: list[Position] = []
        if not self.transport.has_keys:
            return out
        for raw in self._call("fetch_positions", None):
            qty = _dec(raw.get("contracts"))
            if qty == 0:
                continue
            liq = raw.get("liquidationPrice")
            self._liquidation[raw["symbol"]] = _dec(liq) if liq is not None else None
            out.append(
                Position(
                    instrument=raw["symbol"],
                    side="buy" if raw.get("side") == "long" else "sell",
                    qty=qty,
                    entry_price=_dec(raw.get("entryPrice")),
                    leverage=_dec(raw.get("leverage"), Decimal(1)),
                    unrealized_pnl=_dec(raw.get("unrealizedPnl")),
                )
            )
        return out

    def fills(self, since: datetime) -> Sequence[Fill]:
        if self.mode == "paper":
            return [f for f in self._paper_fills if f.ts >= since]
        out: list[Fill] = []
        if not self.transport.has_keys:
            return out
        since_ms = int(since.timestamp() * 1000)
        symbols: list[str | None] = (
            [None] if self.venue == "hyperliquid" else sorted(self._instruments)
        )
        seen: set[str] = set()
        for symbol in symbols:
            for raw in self._call("fetch_my_trades", symbol, since_ms):
                fill = self._parse_fill(raw)
                if fill.id in seen:
                    continue
                seen.add(fill.id)
                out.append(fill)
                self._absorb_fill(fill)
        return out

    def _absorb_fill(self, fill: Fill) -> None:
        order = self._orders.get(fill.order_id)
        if order is None or order.state == OrderState.FILLED:
            return
        filled = min(order.qty, order.filled_qty + fill.qty)
        state = OrderState.FILLED if filled >= order.qty else OrderState.PARTIAL
        self._remember(order.model_copy(update={"filled_qty": filled, "state": state}))

    def balance(self) -> Sequence[Balance]:
        """paper — расчётный баланс из памяти; live — только ответ площадки: при обрыве —
        последний успешный со `stale=True`, без кэша — исключение (цифры не выдумываем)."""
        now = datetime.now(UTC)
        if self.mode == "paper":
            return [
                Balance(
                    asset=self.quote, total=self._paper_balance, free=self._paper_balance, as_of=now
                )
            ]
        if not self.transport.has_keys:
            return []
        try:
            raw = self._call("fetch_balance")
        except ccxt.NetworkError:
            if self._last_balance is None:
                raise
            return [b.model_copy(update={"stale": True}) for b in self._last_balance]
        as_of = _ts(raw.get("timestamp"))
        out = [
            Balance(
                asset=asset,
                total=_dec(total),
                free=_dec((raw.get("free") or {}).get(asset)),
                as_of=as_of,
            )
            for asset, total in (raw.get("total") or {}).items()
            if _dec(total) != 0
        ]
        self._last_balance = out or [
            Balance(asset=self.quote, total=Decimal(0), free=Decimal(0), as_of=as_of)
        ]
        return list(self._last_balance)

    def health(self) -> Health:
        return self.feed.health()

    # -- расширения: перпы, сверка, рестарт -------------------------------------------

    def perp_info(self, instrument: str) -> PerpInfo:
        funding: FundingRate = self.feed.funding(instrument)
        if instrument not in self._liquidation and self.transport.has_keys:
            self.positions()
        return PerpInfo(
            instrument=instrument,
            leverage=self._leverage.get(instrument, Decimal(1)),
            max_leverage=self.max_leverage(instrument),
            funding_rate=funding.rate,
            next_funding_at=funding.next_at,
            mark_price=funding.mark_price,
            liquidation_price=self._liquidation.get(instrument),
        )

    def funding_payments(self, since: datetime) -> list[FundingPayment]:
        if not self.transport.has_keys:
            return []
        rows = self._call("fetch_funding_history", None, int(since.timestamp() * 1000))
        return [
            FundingPayment(
                id=str(r.get("id") or f"{r['symbol']}-{r['timestamp']}"),
                instrument=r["symbol"],
                amount=_dec(r.get("amount")),
                ts=_ts(r.get("timestamp")),
            )
            for r in rows
        ]

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.state in ACTIVE_STATES]

    def reconcile(self, since: datetime) -> ReconcileResult:
        """После обрыва: восполнить филлы, обновить состояния активных ордеров, снять позиции."""
        fills = list(self.fills(since))
        for order in self.open_orders():
            if order.mode == "paper":
                continue
            try:
                self._remember(
                    self._parse_order(self._call("fetch_order", order.id, order.instrument))
                )
            except ccxt.OrderNotFound:
                self._remember(
                    order.model_copy(
                        update={"state": OrderState.CANCELLED, "reason": "нет на площадке"}
                    )
                )
        return ReconcileResult(
            orders=list(self._orders.values()), fills=fills, positions=list(self.positions())
        )

    def restore(self, session: Session) -> list[Order]:
        """Рестарт: активные live-ордера из `orders` → сверка с площадкой → обратно в базу."""
        rows = session.scalars(
            select(OrderRow).where(
                OrderRow.venue == self.venue,
                OrderRow.mode == "live",
                OrderRow.state.in_([s.value for s in ACTIVE_STATES]),
            )
        ).all()
        restored: list[Order] = []
        for row in rows:
            try:
                raw = self._call("fetch_order", row.id, row.instrument)
                order = self._parse_order(raw, signal_id=row.signal_id)
            except ccxt.OrderNotFound:
                found = self._find_on_venue(row.instrument, row.client_order_id)
                order = (
                    found.model_copy(update={"signal_id": row.signal_id})
                    if found
                    else Order(
                        id=row.id,
                        signal_id=row.signal_id,
                        venue=self.venue,
                        client_order_id=row.client_order_id,
                        mode="live",
                        state=OrderState.CANCELLED,
                        instrument=row.instrument,
                        side=row.side,
                        qty=row.qty,
                        price=row.price,
                        filled_qty=row.filled_qty,
                        created_at=row.created_at,
                        reason="нет на площадке",
                    )
                )
            row.state = order.state.value
            row.filled_qty = order.filled_qty
            restored.append(self._remember(order))
        session.flush()
        return restored


class BybitExecutor(CexExecutor):
    venue = "bybit"


class OkxExecutor(CexExecutor):
    venue = "okx"


class BinanceExecutor(CexExecutor):
    venue = "binance"


class HyperliquidExecutor(CexExecutor):
    venue = "hyperliquid"


EXECUTORS: dict[str, type[CexExecutor]] = {
    "bybit": BybitExecutor,
    "okx": OkxExecutor,
    "binance": BinanceExecutor,
    "hyperliquid": HyperliquidExecutor,
}

__all__ = [
    "ACTIVE_STATES",
    "EXECUTORS",
    "BinanceExecutor",
    "BybitExecutor",
    "CexError",
    "CexExecutor",
    "FundingPayment",
    "HyperliquidExecutor",
    "KeyRejected",
    "NotConnected",
    "OkxExecutor",
    "PaperFill",
    "PerpInfo",
    "ReconcileResult",
    "client_order_id",
]
