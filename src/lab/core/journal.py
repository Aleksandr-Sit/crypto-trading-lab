"""core.journal — сигналы/ордера/филлы/сделки, форвард-журнал, сверка (R31i, R31i.1, A03, §10).

Выставляет: `record_signal`, `record_order`, `record_fill`, `close_trade`, `open_qty`, `pnl`,
`reconcile`, `export_csv`. Прячет: связывание филлов в сделки (FIFO по стратегии, инструменту
и режиму — бумажные и живые лоты не смешиваются).

Правила:
- сигнал пишется ДО исхода: `decided_at` и `inputs_hash` обязательны (Signal их требует);
- филл, открывающий позицию, создаёт открытую сделку (лот); филл в обратную сторону закрывает
  лоты по FIFO, частично — сделка делится по объёму, издержки закрытия делятся пропорционально;
- P&L сделки = pnl_gross − издержки по компонентам; unrealized — по переданным маркам;
- сверка ничего не правит: расхождения возвращаются отчётом и событием `ReconcileMismatch`.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.contracts import Costs, Fill, OrderIntent, OrderState, Signal, SignalOutcome
from lab.db.models import FillRow, OrderRow, SignalRow, TradeRow

_COST_FIELDS = tuple(Costs.model_fields)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class SignalRecord(_Model):
    id: str
    strategy_id: str
    decided_at: datetime
    instrument: str
    side: str
    size: Decimal
    inputs_hash: str
    outcome: str
    outcome_at: datetime | None


class OrderRecord(_Model):
    id: str
    signal_id: str
    venue: str
    client_order_id: str
    mode: str
    state: str
    instrument: str
    side: str
    qty: Decimal
    filled_qty: Decimal


class TradeRecord(_Model):
    id: int
    strategy_id: str
    instrument: str
    venue: str
    mode: str
    side: Literal["long", "short"]
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal | None
    open_fill: str
    close_fill: str | None
    pnl_gross: Decimal
    fee: Decimal
    slippage: Decimal
    funding: Decimal
    gas: Decimal
    priority_fee: Decimal
    royalty: Decimal
    pnl_net: Decimal
    opened_at: datetime
    closed_at: datetime | None

    @property
    def costs(self) -> Costs:
        return Costs(
            fee=self.fee,
            slippage=self.slippage,
            funding=self.funding,
            gas=self.gas,
            priority_fee=self.priority_fee,
            royalty=self.royalty,
        )

    @classmethod
    def from_row(cls, r: TradeRow) -> TradeRecord:
        return cls(
            id=r.id,
            strategy_id=r.strategy_id,
            instrument=r.instrument,
            venue=r.venue,
            mode=r.mode,
            side=r.side,
            qty=r.qty,
            entry_price=r.entry_price,  # type: ignore[arg-type]
            exit_price=r.exit_price,
            open_fill=r.open_fill,
            close_fill=r.close_fill,
            pnl_gross=r.pnl_gross,
            fee=r.fee,
            slippage=r.slippage,
            funding=r.funding,
            gas=r.gas,
            priority_fee=r.priority_fee,
            royalty=r.royalty,
            pnl_net=r.pnl_net,
            opened_at=r.opened_at,
            closed_at=r.closed_at,
        )


class PnL(_Model):
    realized: Decimal
    unrealized: Decimal
    n_closed: int
    n_open: int

    @property
    def total(self) -> Decimal:
        return self.realized + self.unrealized


class Mismatch(_Model):
    kind: Literal["missing_in_journal", "missing_on_venue", "price", "qty", "fee"]
    fill_id: str
    ours: Decimal | None = None
    theirs: Decimal | None = None


class ReconcileMismatch(_Model):
    """Событие сверки: расхождение с площадкой выше порога — тревога, не тихая правка."""

    venue: str
    checked_at: datetime
    count: int
    mismatches: list[Mismatch]


class ReconcileReport(_Model):
    venue: str
    checked: int
    ok: bool
    mismatches: list[Mismatch]
    event: ReconcileMismatch | None


CSV_COLUMNS = (
    "trade_id",
    "strategy_id",
    "instrument",
    "side",
    "qty",
    "entry_price",
    "exit_price",
    "pnl_gross",
    "fee",
    "slippage",
    "funding",
    "gas",
    "priority_fee",
    "royalty",
    "pnl_net",
    "opened_at",
    "closed_at",
    "venue",
    "mode",
    "open_fill",
    "close_fill",
)


def _scale(c: Costs, k: Decimal) -> Costs:
    return Costs(**{f: getattr(c, f) * k for f in _COST_FIELDS})


class Journal:
    def __init__(self, session: Session) -> None:
        self.s = session

    # -- сигналы и ордера ------------------------------------------------------------

    def record_signal(self, signal: Signal, signal_id: str | None = None) -> SignalRecord:
        row = SignalRow(
            id=signal_id or f"sig-{uuid4().hex[:16]}",
            strategy_id=signal.strategy_id,
            decided_at=signal.decided_at,
            instrument=signal.instrument,
            side=signal.side,
            size=signal.size,
            price_ref=signal.price_ref,
            inputs_hash=signal.inputs_hash,
            ttl=signal.ttl_s,
            meta=dict(signal.meta),
        )
        self.s.add(row)
        self.s.flush()
        return self._signal(row)

    def signal(self, signal_id: str) -> SignalRecord:
        return self._signal(self.s.get_one(SignalRow, signal_id))

    def set_outcome(self, signal_id: str, outcome: SignalOutcome | str, at: datetime) -> None:
        row = self.s.get_one(SignalRow, signal_id)
        row.outcome = SignalOutcome(outcome).value
        row.outcome_at = at
        self.s.flush()

    def record_order(
        self,
        intent: OrderIntent,
        *,
        order_id: str,
        state: OrderState = OrderState.NEW,
        risk_verdict: dict | None = None,
    ) -> OrderRecord:
        row = OrderRow(
            id=order_id,
            signal_id=intent.signal_id,
            venue=intent.venue,
            client_order_id=intent.client_order_id,
            mode=intent.mode,
            state=state.value,
            instrument=intent.instrument,
            side=intent.side,
            order_type=intent.order_type,
            qty=intent.qty,
            price=intent.price,
            leverage=intent.leverage,
            reduce_only=intent.reduce_only,
            risk_verdict=risk_verdict or {},
        )
        self.s.add(row)
        self.s.flush()
        return self._order(row)

    # -- филлы и сделки ------------------------------------------------------------------

    def record_fill(
        self,
        fill: Fill,
        *,
        costs: Costs | None = None,
        ref_price: Decimal | None = None,
    ) -> list[TradeRecord]:
        """Записать филл и связать его в сделки: возвращает закрытые этим филлом сделки."""
        order = self.s.get_one(OrderRow, fill.order_id)
        signal = self.s.get_one(SignalRow, order.signal_id)
        costs = costs or Costs(fee=fill.fee)
        self.s.add(
            FillRow(
                id=fill.id,
                order_id=fill.order_id,
                price=fill.price,
                qty=fill.qty,
                fee=fill.fee,
                fee_asset=fill.fee_asset,
                ts=fill.ts,
                ref_price=ref_price,
                costs_json=costs.model_dump(mode="json"),
            )
        )
        order.filled_qty = order.filled_qty + fill.qty
        order.state = (
            OrderState.FILLED if order.filled_qty >= order.qty else OrderState.PARTIAL
        ).value
        if signal.outcome == SignalOutcome.PENDING.value:
            signal.outcome = SignalOutcome.EXECUTED.value
            signal.outcome_at = fill.ts
        self.s.flush()
        return self._link(signal.strategy_id, order, fill, costs)

    def _link(
        self, strategy_id: str, order: OrderRow, fill: Fill, costs: Costs
    ) -> list[TradeRecord]:
        opening = "long" if order.side == "buy" else "short"
        closing = "short" if order.side == "buy" else "long"
        open_rows = self.s.scalars(
            select(TradeRow)
            .where(
                TradeRow.strategy_id == strategy_id,
                TradeRow.instrument == order.instrument,
                TradeRow.mode == order.mode,  # живой филл не закрывает бумажный лот
                TradeRow.closed_at.is_(None),
                TradeRow.side == closing,
            )
            .order_by(TradeRow.opened_at, TradeRow.id)
        ).all()
        remaining = fill.qty
        closed: list[TradeRecord] = []
        for row in open_rows:
            if remaining <= 0:
                break
            take = min(remaining, row.qty)
            if take < row.qty:
                row = self._split(row, take)
            closed.append(self._close(row, fill, _scale(costs, take / fill.qty)))
            remaining -= take
        if remaining > 0:
            self.s.add(
                TradeRow(
                    strategy_id=strategy_id,
                    instrument=order.instrument,
                    venue=order.venue,
                    mode=order.mode,
                    side=opening,
                    qty=remaining,
                    entry_price=fill.price,
                    open_fill=fill.id,
                    close_fill=None,
                    pnl_gross=Decimal(0),
                    pnl_net=Decimal(0),
                    opened_at=fill.ts,
                    closed_at=None,
                    **{f: getattr(_scale(costs, remaining / fill.qty), f) for f in _COST_FIELDS},
                )
            )
        self.s.flush()
        return closed

    def _split(self, row: TradeRow, take: Decimal) -> TradeRow:
        """Отделить от открытого лота часть объёма `take` (издержки открытия — пропорционально)."""
        k = take / row.qty
        part = TradeRow(
            strategy_id=row.strategy_id,
            instrument=row.instrument,
            venue=row.venue,
            mode=row.mode,
            side=row.side,
            qty=take,
            entry_price=row.entry_price,
            open_fill=row.open_fill,
            close_fill=None,
            pnl_gross=Decimal(0),
            pnl_net=Decimal(0),
            opened_at=row.opened_at,
            closed_at=None,
            **{f: getattr(row, f) * k for f in _COST_FIELDS},
        )
        for f in _COST_FIELDS:
            setattr(row, f, getattr(row, f) * (1 - k))
        row.qty = row.qty - take
        self.s.add(part)
        self.s.flush()
        return part

    def _close(self, row: TradeRow, fill: Fill, close_costs: Costs) -> TradeRecord:
        direction = 1 if row.side == "long" else -1
        row.exit_price = fill.price
        row.close_fill = fill.id
        row.closed_at = fill.ts
        row.pnl_gross = (fill.price - row.entry_price) * row.qty * direction
        for f in _COST_FIELDS:
            setattr(row, f, getattr(row, f) + getattr(close_costs, f))
        row.pnl_net = row.pnl_gross - sum((getattr(row, f) for f in _COST_FIELDS), Decimal(0))
        self.s.flush()
        return TradeRecord.from_row(row)

    def close_trade(self, trade_id: int, fill: Fill, *, costs: Costs | None = None) -> TradeRecord:
        """Закрыть конкретную открытую сделку этим филлом (объём филла ≥ объёма сделки)."""
        row = self.s.get_one(TradeRow, trade_id)
        if row.closed_at is not None:
            raise ValueError(f"сделка {trade_id} уже закрыта")
        if fill.qty < row.qty:
            raise ValueError("объём филла меньше объёма сделки: используй record_fill (FIFO)")
        costs = costs or Costs(fee=fill.fee)
        if self.s.get(FillRow, fill.id) is None:
            self.s.add(
                FillRow(
                    id=fill.id,
                    order_id=fill.order_id,
                    price=fill.price,
                    qty=fill.qty,
                    fee=fill.fee,
                    fee_asset=fill.fee_asset,
                    ts=fill.ts,
                    costs_json=costs.model_dump(mode="json"),
                )
            )
            self.s.flush()
        return self._close(row, fill, _scale(costs, row.qty / fill.qty))

    # -- чтение ----------------------------------------------------------------------------

    def open_trades(self, strategy_id: str) -> list[TradeRecord]:
        return [TradeRecord.from_row(r) for r in self._trades(strategy_id, closed=False)]

    def open_qty(self, strategy_id: str, instrument: str, side: str, *, mode: str) -> Decimal:
        """Объём открытых лотов стратегии по инструменту и стороне (`long`/`short`) в режиме."""
        rows = self.s.scalars(
            select(TradeRow.qty).where(
                TradeRow.strategy_id == strategy_id,
                TradeRow.instrument == instrument,
                TradeRow.side == side,
                TradeRow.mode == mode,
                TradeRow.closed_at.is_(None),
            )
        ).all()
        return sum(rows, Decimal(0))

    def closed_trades(self, strategy_id: str) -> list[TradeRecord]:
        return [TradeRecord.from_row(r) for r in self._trades(strategy_id, closed=True)]

    def _trades(self, strategy_id: str | None, closed: bool | None = None) -> list[TradeRow]:
        q = select(TradeRow)
        if strategy_id is not None:
            q = q.where(TradeRow.strategy_id == strategy_id)
        if closed is True:
            q = q.where(TradeRow.closed_at.is_not(None))
        elif closed is False:
            q = q.where(TradeRow.closed_at.is_(None))
        return list(self.s.scalars(q.order_by(TradeRow.opened_at, TradeRow.id)).all())

    def pnl(self, strategy_id: str, *, marks: dict[str, Decimal] | None = None) -> PnL:
        """P&L = realized (закрытые) + unrealized (открытые по маркам, минус их издержки)."""
        marks = marks or {}
        realized = Decimal(0)
        unrealized = Decimal(0)
        n_closed = n_open = 0
        for r in self._trades(strategy_id):
            if r.closed_at is not None:
                realized += r.pnl_net
                n_closed += 1
                continue
            n_open += 1
            mark = marks.get(r.instrument)
            if mark is None:
                continue
            direction = 1 if r.side == "long" else -1
            gross = (mark - r.entry_price) * r.qty * direction
            unrealized += gross - sum((getattr(r, f) for f in _COST_FIELDS), Decimal(0))
        return PnL(realized=realized, unrealized=unrealized, n_closed=n_closed, n_open=n_open)

    def export_csv(
        self,
        *,
        strategy_id: str | None = None,
        closed: bool | None = None,
        since: datetime | None = None,
    ) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(CSV_COLUMNS)
        for r in self._trades(strategy_id, closed):
            if since is not None and r.opened_at < since:
                continue
            w.writerow(
                [
                    r.id,
                    r.strategy_id,
                    r.instrument,
                    r.side,
                    r.qty,
                    r.entry_price,
                    "" if r.exit_price is None else r.exit_price,
                    r.pnl_gross,
                    r.fee,
                    r.slippage,
                    r.funding,
                    r.gas,
                    r.priority_fee,
                    r.royalty,
                    r.pnl_net,
                    r.opened_at.isoformat(),
                    "" if r.closed_at is None else r.closed_at.isoformat(),
                    r.venue,
                    r.mode,
                    r.open_fill,
                    r.close_fill or "",
                ]
            )
        return buf.getvalue()

    # -- сверка --------------------------------------------------------------------------

    def reconcile(
        self,
        venue: str,
        fills_from_venue: list[Fill],
        *,
        since: datetime,
        tolerance: Decimal = Decimal("0.0001"),
        now: datetime | None = None,
    ) -> ReconcileReport:
        """Сравнить филлы площадки с журналом с `since`. Расхождение по цене/объёму/комиссии
        больше `tolerance` (доля) или отсутствующий филл → Mismatch; ничего не правится."""
        ours = {
            f.id: f
            for f in self.s.scalars(
                select(FillRow)
                .join(OrderRow, FillRow.order_id == OrderRow.id)
                .where(OrderRow.venue == venue, FillRow.ts >= since)
            ).all()
        }
        theirs = {f.id: f for f in fills_from_venue if f.ts >= since}
        mismatches: list[Mismatch] = []

        def differs(a: Decimal, b: Decimal) -> bool:
            base = max(abs(a), abs(b))
            return base > 0 and abs(a - b) / base > tolerance

        for fid, their in theirs.items():
            our = ours.get(fid)
            if our is None:
                mismatches.append(
                    Mismatch(kind="missing_in_journal", fill_id=fid, theirs=their.price)
                )
                continue
            for kind, a, b in (
                ("price", our.price, their.price),
                ("qty", our.qty, their.qty),
                ("fee", our.fee, their.fee),
            ):
                if differs(a, b):
                    mismatches.append(Mismatch(kind=kind, fill_id=fid, ours=a, theirs=b))  # type: ignore[arg-type]
        for fid, our in ours.items():
            if fid not in theirs:
                mismatches.append(Mismatch(kind="missing_on_venue", fill_id=fid, ours=our.price))
        checked_at = now or datetime.now(tz=since.tzinfo)
        event = None
        if mismatches:
            event = ReconcileMismatch(
                venue=venue, checked_at=checked_at, count=len(mismatches), mismatches=mismatches
            )
        return ReconcileReport(
            venue=venue,
            checked=len(theirs) + len(ours),
            ok=not mismatches,
            mismatches=mismatches,
            event=event,
        )

    # -- маппинг ---------------------------------------------------------------------------

    @staticmethod
    def _signal(r: SignalRow) -> SignalRecord:
        return SignalRecord(
            id=r.id,
            strategy_id=r.strategy_id,
            decided_at=r.decided_at,
            instrument=r.instrument,
            side=r.side,
            size=r.size,
            inputs_hash=r.inputs_hash,
            outcome=r.outcome,
            outcome_at=r.outcome_at,
        )

    @staticmethod
    def _order(r: OrderRow) -> OrderRecord:
        return OrderRecord(
            id=r.id,
            signal_id=r.signal_id,
            venue=r.venue,
            client_order_id=r.client_order_id,
            mode=r.mode,
            state=r.state,
            instrument=r.instrument,
            side=r.side,
            qty=r.qty,
            filled_qty=r.filled_qty,
        )


__all__ = [
    "CSV_COLUMNS",
    "Journal",
    "Mismatch",
    "OrderRecord",
    "PnL",
    "ReconcileMismatch",
    "ReconcileReport",
    "SignalRecord",
    "TradeRecord",
]
