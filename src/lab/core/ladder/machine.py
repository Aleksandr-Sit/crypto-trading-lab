"""core.ladder — конечный автомат ступеней доверия в ядре, не в стратегиях (решение §7).

Выставляет: `evaluate(strategy_id, metrics=None) -> Transition | None`, `promote(id, by)`,
`demote(id, reason)`, `breach(id, reason, snapshot)`, `halt_all()`, `resume_all()`,
`expire_signals(now)`, `history(id)`. Порог — через интерфейс `threshold(metrics, branch, rung)`,
реализация core.measure подставляется вызывающим. Каждый переход — строка `rung_transitions`
с цифрами (`metrics_snapshot`) и тем, кто его вызвал (`by`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.contracts import Branch, Rung, SignalOutcome, Status
from lab.core.ladder.rules import (
    DEMOTE_PREV,
    MEASURE_MODE_FOR_RUNG,
    SIGNAL_RUNGS,
    By,
    initial_rung,
    next_rung,
)
from lab.core.measure.types import Metrics, ThresholdResult
from lab.core.registry import StrategyNotFound
from lab.core.risk.state import MemoryHaltSwitch
from lab.core.risk.types import HaltSwitch
from lab.db.base import utcnow
from lab.db.models import MeasurementRow, RungTransitionRow, SignalRow, StrategyRow

ThresholdFn = Callable[[Metrics, Branch, Rung], ThresholdResult]
MetricsSource = Callable[[str, Rung], Metrics | None]
CancelOrders = Callable[[str], int]

_FROZEN = frozenset({Status.RETIRED, Status.DEGRADED})
_SNAPSHOT_FIELDS = (
    "n_trades",
    "ev_per_trade",
    "ev_ci95",
    "net_pnl_pct",
    "max_dd_pct",
    "vs_btc",
    "win_rate",
    "costs_pct",
    "window_from",
    "window_to",
)


class LadderError(Exception):
    pass


class OperatorRequired(LadderError):
    """Переход разрешён только оператору (`semi → auto`)."""


class Transition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int | None = None
    strategy_id: str
    from_rung: Rung
    to_rung: Rung
    status: Status
    reason: str
    by: By
    metrics_snapshot: dict[str, Any] = Field(default_factory=dict)
    ts: datetime

    @classmethod
    def from_row(cls, row: RungTransitionRow, status: Status) -> Transition:
        return cls(
            id=row.id,
            strategy_id=row.strategy_id,
            from_rung=Rung(row.from_rung),
            to_rung=Rung(row.to_rung),
            status=status,
            reason=row.reason,
            by=row.by,  # type: ignore[arg-type]
            metrics_snapshot=dict(row.metrics_snapshot),
            ts=row.ts,
        )


def metrics_snapshot(metrics: Metrics | None, result: ThresholdResult | None) -> dict[str, Any]:
    """Цифры, вызвавшие переход (R02.3/R02.4): ключевые метрики + критерии порога.
    JSON-совместимо."""
    snap: dict[str, Any] = {}
    if metrics is not None:
        dumped = metrics.model_dump(mode="json")
        snap["metrics"] = {k: dumped[k] for k in _SNAPSHOT_FIELDS}
    if result is not None:
        snap["threshold"] = result.model_dump(mode="json")
    return snap


class Ladder:
    def __init__(
        self,
        session: Session,
        *,
        threshold: ThresholdFn,
        halt: HaltSwitch | None = None,
        metrics: MetricsSource | None = None,
        cancel_orders: CancelOrders | None = None,
        notify: Callable[[Transition], None] | None = None,
    ) -> None:
        self.s = session
        self._threshold = threshold
        self._halt = halt or MemoryHaltSwitch()
        self._metrics = metrics or self._latest_metrics
        self._cancel = cancel_orders
        self._notify = notify

    # -- переходы ----------------------------------------------------------------------

    def start(self, strategy_id: str) -> Transition | None:
        """Начальная ступень при первом запуске: `paper` для форвард-только стратегий,
        иначе как есть. Статус `candidate` → `measuring`."""
        row = self._row(strategy_id)
        if row.status in _FROZEN:
            return None
        transition = None
        first = initial_rung(row.can_backtest)
        if row.status == Status.CANDIDATE and Rung(row.rung) != first:
            transition = self._move(
                row,
                first,
                Status.MEASURING,
                "форвард-только: бэктеста нет, старт с paper",
                "system",
            )
        if row.status == Status.CANDIDATE:
            row.status = Status.MEASURING.value
            self.s.flush()
        return transition

    def evaluate(self, strategy_id: str, metrics: Metrics | None = None) -> Transition | None:
        row = self._row(strategy_id)
        if row.status in _FROZEN:
            return None
        started = self.start(strategy_id)
        rung = Rung(row.rung)
        m = metrics if metrics is not None else self._metrics(strategy_id, rung)
        if m is None:
            return started
        result = self._threshold(m, Branch(row.branch), rung)
        snap = metrics_snapshot(m, result)

        if result.status == "passed":
            row.status = Status.PASSED.value
            target = next_rung(rung, by="system")
            if target is None:
                self.s.flush()
                return started
            return self._move(
                row, target, Status.MEASURING, f"порог пройден на {rung}", "system", snap
            )

        if result.status == "failed":
            row.status = Status.FAILED.value
            failed = ", ".join(result.failed_names()) or "порог"
            target = DEMOTE_PREV[rung]
            if target is None:
                self.s.flush()
                return started
            return self._move(
                row, target, Status.MEASURING, f"порог не пройден: {failed}", "system", snap
            )

        row.status = Status.MEASURING.value
        self.s.flush()
        return started

    def promote(
        self, strategy_id: str, by: By, reason: str = "", snapshot: dict[str, Any] | None = None
    ) -> Transition:
        row = self._row(strategy_id)
        rung = Rung(row.rung)
        target = next_rung(rung, by=by)
        if target is None:
            if next_rung(rung, by="operator") is not None:
                raise OperatorRequired(
                    f"{rung} → {next_rung(rung, by='operator')}: только оператор"
                )
            raise LadderError(f"{strategy_id} уже на верхней ступени {rung}")
        return self._move(
            row, target, Status.MEASURING, reason or f"поднята вручную с {rung}", by, snapshot or {}
        )

    def demote(
        self,
        strategy_id: str,
        reason: str,
        *,
        by: By = "system",
        snapshot: dict[str, Any] | None = None,
    ) -> Transition | None:
        row = self._row(strategy_id)
        rung = Rung(row.rung)
        target = DEMOTE_PREV[rung]
        if target is None:
            return None
        return self._move(row, target, Status.MEASURING, reason, by, snapshot or {})

    def breach(
        self, strategy_id: str, reason: str, snapshot: dict[str, Any] | None = None
    ) -> Transition:
        """Пробой стопа стратегии (G04): статус `degraded`, её ордера отменяются,
        ступень не меняется."""
        row = self._row(strategy_id)
        snap = dict(snapshot or {})
        snap["cancelled_orders"] = self._cancel(strategy_id) if self._cancel else 0
        rung = Rung(row.rung)
        return self._move(row, rung, Status.DEGRADED, f"пробой стопа: {reason}", "system", snap)

    # -- «стоп всё» --------------------------------------------------------------------

    def halt_all(self, by: str = "operator") -> None:
        self._halt.halt(by)

    def resume_all(self, by: str = "operator") -> None:
        self._halt.resume(by)

    @property
    def halted(self) -> bool:
        return self._halt.is_halted()

    # -- сигналы на signal/semi ---------------------------------------------------------

    def expire_signals(self, now: datetime | None = None) -> list[str]:
        """Сигналы без ответа оператора дольше ttl → `expired` (R03.1). ttl — из манифеста
        (`params.ttl_s`), иначе ttl самого сигнала."""
        now = now or utcnow()
        stmt = (
            select(SignalRow, StrategyRow)
            .join(StrategyRow, StrategyRow.id == SignalRow.strategy_id)
            .where(
                SignalRow.outcome == SignalOutcome.PENDING.value,
                StrategyRow.rung.in_([r.value for r in SIGNAL_RUNGS]),
            )
            .order_by(SignalRow.decided_at)
        )
        expired: list[str] = []
        for sig, strat in self.s.execute(stmt).all():
            ttl = int(strat.params_json.get("ttl_s", sig.ttl))
            if (now - sig.decided_at).total_seconds() > ttl:
                sig.outcome = SignalOutcome.EXPIRED.value
                sig.outcome_at = now
                expired.append(sig.id)
        self.s.flush()
        return expired

    # -- история -----------------------------------------------------------------------

    def history(self, strategy_id: str) -> list[Transition]:
        row = self._row(strategy_id)
        stmt = (
            select(RungTransitionRow)
            .where(RungTransitionRow.strategy_id == strategy_id)
            .order_by(RungTransitionRow.ts, RungTransitionRow.id)
        )
        return [Transition.from_row(r, Status(row.status)) for r in self.s.scalars(stmt).all()]

    # -- внутреннее --------------------------------------------------------------------

    def _row(self, strategy_id: str) -> StrategyRow:
        row = self.s.get(StrategyRow, strategy_id)
        if row is None:
            raise StrategyNotFound(f"стратегия {strategy_id} не найдена")
        return row

    def _move(
        self,
        row: StrategyRow,
        to: Rung,
        status: Status,
        reason: str,
        by: By,
        snapshot: dict[str, Any] | None = None,
    ) -> Transition:
        rec = RungTransitionRow(
            strategy_id=row.id,
            from_rung=row.rung,
            to_rung=to.value,
            reason=reason,
            metrics_snapshot=dict(snapshot or {}),
            ts=utcnow(),
            by=by,
        )
        row.rung = to.value
        row.status = status.value
        self.s.add(rec)
        self.s.flush()
        t = Transition.from_row(rec, status)
        if self._notify is not None:
            self._notify(t)
        return t

    def _latest_metrics(self, strategy_id: str, rung: Rung) -> Metrics | None:
        """Последний удачный замер стратегии в режиме, отвечающем за ступень."""
        stmt = (
            select(MeasurementRow)
            .where(
                MeasurementRow.strategy_id == strategy_id,
                MeasurementRow.mode == MEASURE_MODE_FOR_RUNG[rung].value,
                MeasurementRow.status == "ok",
            )
            .order_by(MeasurementRow.created_at.desc(), MeasurementRow.id.desc())
            .limit(1)
        )
        row = self.s.scalar(stmt)
        if row is None or not row.metrics_json.get("metrics"):
            return None
        return Metrics.model_validate(row.metrics_json["metrics"])
