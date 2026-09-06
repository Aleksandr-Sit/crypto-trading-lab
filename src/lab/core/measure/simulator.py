"""Событийный симулятор и бумажный движок (решения §3, §6, §10; истории 17–18).

Один движок исполнения на бэктест и бумагу — `PaperEngine`:
  submit(signal)  — принять сигнал; decided_at раньше уже известных данных → LookaheadError;
  on_bar(bar)     — исполнить ожидающие сигналы по этому бару, начислить фандинг, вернуть филлы.

Правила исполнения:
  market — по открытию бара ± проскальзывание из модели издержек (цена хуже мида);
  limit  — если бар коснулся цены; частично: не больше max_participation × объёма бара за бар;
  фандинг — для перпов раз в funding_interval_h по открытой позиции (лонг платит при rate > 0).
P&L сделки считается по референсным ценам (мид), проскальзывание — отдельная компонента издержек,
чтобы не считать его дважды.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import count

from lab.contracts import Branch, Candle, Costs, Fill, OrderIntent, Signal
from lab.contracts.timeframes import parse_tf
from lab.core.costs import CostModel, Depth, default_model
from lab.core.measure.types import ClosedTrade, IncompleteData, LookaheadError

PERP_BRANCHES = {Branch.CEX_PERP, Branch.DEX_PERP}


def check_continuity(candles: Sequence[Candle], tf: timedelta) -> None:
    """Свечи должны идти подряд с шагом tf — иначе замер `incomplete` (R11.6)."""
    if not candles:
        raise IncompleteData("нет свечей в окне")
    for prev, cur in zip(candles, candles[1:], strict=False):
        if cur.ts - prev.ts != tf:
            raise IncompleteData(
                f"разрыв данных между {prev.ts.isoformat()} и {cur.ts.isoformat()} "
                f"(ожидался шаг {tf})"
            )


@dataclass
class _Lot:
    side: str  # long | short
    qty: Decimal
    ref_price: Decimal
    opened_at: datetime
    costs: Costs


@dataclass
class _Pending:
    signal: Signal
    remaining: Decimal
    expires_at: datetime
    order_type: str
    limit_price: Decimal | None
    order_id: str = ""


@dataclass
class PaperEngine:
    venue: str
    instrument: str
    tf: str
    branch: Branch | str = Branch.CEX_SPOT
    costs: CostModel = field(default_factory=default_model)
    funding_rate: Decimal = Decimal("0.0001")
    max_participation: Decimal = Decimal("0.1")
    depth: Depth | None = None
    quote_asset: str = "USD"
    on_fill: Callable[[Fill, Signal, Costs, Decimal], None] | None = None

    def __post_init__(self) -> None:
        self.step = parse_tf(self.tf)
        self.clock: datetime | None = None
        self.pending: list[_Pending] = []
        self.lots: list[_Lot] = []
        self.fills: list[Fill] = []
        self.closed: list[ClosedTrade] = []
        self.expired: list[Signal] = []
        self._ids = count(1)
        self._funding_h = self.costs.funding_interval_h(self.venue) or 8
        self._is_perp = Branch(self.branch) in PERP_BRANCHES

    # -- вход --------------------------------------------------------------------

    @property
    def position(self) -> Decimal:
        return sum((lot.qty if lot.side == "long" else -lot.qty for lot in self.lots), Decimal(0))

    def submit(self, signal: Signal) -> None:
        if self.clock is not None and signal.decided_at < self.clock:
            raise LookaheadError(
                f"сигнал решён {signal.decided_at.isoformat()}, а движок уже видел данные до "
                f"{self.clock.isoformat()} — заглядывание в будущее"
            )
        if signal.side not in ("buy", "sell"):
            raise ValueError(f"side {signal.side!r}: ожидается buy|sell")
        if signal.size <= 0:
            raise ValueError("size должен быть > 0")
        order_type = str(signal.meta.get("order_type", "market"))
        limit_price = signal.meta.get(
            "limit_price", signal.price_ref if order_type == "limit" else None
        )
        self.pending.append(
            _Pending(
                signal=signal,
                remaining=signal.size,
                expires_at=signal.decided_at + timedelta(seconds=signal.ttl_s),
                order_type=order_type,
                limit_price=None if limit_price is None else Decimal(str(limit_price)),
                order_id=f"paper-{next(self._ids)}",
            )
        )

    def on_bar(self, bar: Candle) -> list[Fill]:
        if self.clock is not None and bar.ts < self.clock:
            raise LookaheadError(f"бар {bar.ts.isoformat()} раньше часов движка {self.clock}")
        fills: list[Fill] = []
        still: list[_Pending] = []
        for p in self.pending:
            if p.signal.decided_at > bar.ts:
                still.append(p)
                continue
            if bar.ts >= p.expires_at and p.expires_at > p.signal.decided_at:
                self.expired.append(p.signal)
                continue
            fill = self._execute(p, bar)
            if fill is not None:
                fills.append(fill)
            if p.remaining > 0:
                still.append(p)
        self.pending = still
        if self._is_perp:
            self._accrue_funding(bar)
        self.clock = bar.ts + self.step
        return fills

    # -- исполнение -----------------------------------------------------------------

    def _intent(self, p: _Pending, qty: Decimal, price: Decimal | None) -> OrderIntent:
        return OrderIntent(
            strategy_id=p.signal.strategy_id,
            venue=self.venue,
            instrument=self.instrument,
            side=p.signal.side,  # type: ignore[arg-type]
            qty=qty,
            price=price,
            order_type="limit" if p.order_type == "limit" else "market",
            mode="paper",
            signal_id=p.signal.inputs_hash,
            client_order_id=p.order_id,
        )

    def _execute(self, p: _Pending, bar: Candle) -> Fill | None:
        side = p.signal.side
        if p.order_type == "limit":
            price = p.limit_price
            if price is None:
                raise ValueError("лимитному сигналу нужна limit_price или price_ref")
            touched = bar.low <= price if side == "buy" else bar.high >= price
            if not touched:
                return None
            cap = bar.volume * self.max_participation
            qty = min(p.remaining, cap) if cap > 0 else p.remaining
            if qty <= 0:
                return None
            ref = price
            costs = self.costs.estimate(self.venue, self._intent(p, qty, price), depth=self.depth)
            fill_price = price
        else:
            qty = p.remaining
            ref = bar.open
            intent = self._intent(p, qty, ref)
            costs = self.costs.estimate(self.venue, intent, depth=self.depth)
            sign = 1 if side == "buy" else -1
            fill_price = ref + sign * costs.slippage / qty
        p.remaining -= qty
        fill = Fill(
            id=f"pf-{next(self._ids)}",
            order_id=p.order_id,
            price=fill_price,
            qty=qty,
            fee=costs.fee,
            fee_asset=self.quote_asset,
            ts=bar.ts,
        )
        self.fills.append(fill)
        self._apply(side, qty, ref, bar.ts, costs)
        if self.on_fill is not None:
            self.on_fill(fill, p.signal, costs, ref)
        return fill

    def _apply(self, side: str, qty: Decimal, ref: Decimal, ts: datetime, costs: Costs) -> None:
        opening = "long" if side == "buy" else "short"
        closing = "short" if side == "buy" else "long"
        remaining = qty
        total_qty = qty
        while remaining > 0 and self.lots and self.lots[0].side == closing:
            lot = self.lots[0]
            take = min(remaining, lot.qty)
            share_open = take / lot.qty  # доля от ОСТАТКА лота: издержки не теряются и не двоятся
            share_close = take / total_qty
            lot_costs = _scale(lot.costs, share_open)
            close_costs = _scale(costs, share_close)
            direction = 1 if lot.side == "long" else -1
            self.closed.append(
                ClosedTrade(
                    instrument=self.instrument,
                    side=lot.side,  # type: ignore[arg-type]
                    qty=take,
                    entry_price=lot.ref_price,
                    exit_price=ref,
                    opened_at=lot.opened_at,
                    closed_at=ts,
                    pnl_gross=(ref - lot.ref_price) * take * direction,
                    costs=_add(lot_costs, close_costs),
                )
            )
            lot.costs = _scale(lot.costs, 1 - share_open)
            lot.qty -= take
            remaining -= take
            if lot.qty <= 0:
                self.lots.pop(0)
        if remaining > 0:
            self.lots.append(
                _Lot(
                    side=opening,
                    qty=remaining,
                    ref_price=ref,
                    opened_at=ts,
                    costs=_scale(costs, remaining / total_qty),
                )
            )

    def _accrue_funding(self, bar: Candle) -> None:
        """Начисление за все границы фандинга внутри бара [ts, ts+step): на дневных барах
        при интервале 8 ч их три, на часовых — ноль или одна."""
        periods = _funding_periods(bar.ts, bar.ts + self.step, self._funding_h)
        if not periods or not self.lots:
            return
        for lot in self.lots:
            sign = 1 if lot.side == "long" else -1
            pay = self.funding_rate * lot.qty * bar.open * sign * periods
            lot.costs = lot.costs.model_copy(update={"funding": lot.costs.funding + pay})


def _funding_periods(start: datetime, end: datetime, interval_h: int) -> int:
    """Сколько границ фандинга (часы, кратные интервалу от полуночи UTC) попало в [start, end)."""
    step = interval_h * 3600
    a, b = int(start.timestamp()), int(end.timestamp())
    return max(0, (b - 1) // step - (a - 1) // step)


def _scale(c: Costs, k: Decimal) -> Costs:
    return Costs(**{name: getattr(c, name) * k for name in Costs.model_fields})


def _add(a: Costs, b: Costs) -> Costs:
    return Costs(**{name: getattr(a, name) + getattr(b, name) for name in Costs.model_fields})


@dataclass(frozen=True)
class SimResult:
    trades: list[ClosedTrade]
    fills: list[Fill]
    open_position: Decimal
    expired_signals: int


def simulate(
    strategy,
    candles: Sequence[Candle],
    *,
    engine: PaperEngine,
) -> SimResult:
    """Прогон стратегии по свечам: сначала исполняются ожидающие сигналы по бару,
    потом стратегия видит бар и решает — её сигналы исполнятся не раньше следующего бара."""
    check_continuity(candles, engine.step)
    for bar in candles:
        engine.on_bar(bar)
        for signal in strategy.on_bar(bar):
            engine.submit(signal)
    return SimResult(
        trades=list(engine.closed),
        fills=list(engine.fills),
        open_position=engine.position,
        expired_signals=len(engine.expired),
    )
