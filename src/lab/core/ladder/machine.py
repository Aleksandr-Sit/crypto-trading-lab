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
    "execution",
    "trade_source",
    "forward_verified",
    "observation_id",
    "rules_revision",
    "observation_started_at",
    "forward_excluded",
    "virtual_state",
    "stability",
    "stability_error",
    "n_trades",
    "ev_per_trade",
    "cost_stress",
    "ev_ci95",
    "ev_search_ci",
    "bootstrap",
    "research",
    "net_pnl_pct",
    "max_dd_pct",
    "vs_benchmark",
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


def default_threshold_fn() -> ThresholdFn:
    """Адаптер к настоящему `core.measure.threshold(m, branch, *, rung=)` под интерфейс лестницы."""
    from lab.core.measure import threshold as measure_threshold

    def fn(metrics: Metrics, branch: Branch, rung: Rung) -> ThresholdResult:
        return measure_threshold(metrics, branch, rung=rung)

    return fn


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
        if Rung(row.rung) != Rung.BACKTEST:
            from lab.core.forward import current_observation

            current_observation(self.s, strategy_id, start=True)
        return transition

    def latest_metrics(self, strategy_id: str) -> Metrics | None:
        """Метрики, по которым `evaluate` судил бы стратегию на её текущей ступени."""
        row = self._row(strategy_id)
        return self._metrics(strategy_id, Rung(row.rung))

    def evaluate(self, strategy_id: str, metrics: Metrics | None = None) -> Transition | None:
        row = self._row(strategy_id)
        if row.status in _FROZEN:
            return None
        started = self.start(strategy_id)
        rung = Rung(row.rung)
        m = metrics if metrics is not None else self._metrics(strategy_id, rung)
        if m is None:
            return started
        result = self._admission(row, m, rung)
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
        row = self._movable(strategy_id)
        rung = Rung(row.rung)
        target = next_rung(rung, by=by)
        if target is None:
            if next_rung(rung, by="operator") is not None:
                raise OperatorRequired(
                    f"{rung} → {next_rung(rung, by='operator')}: только оператор"
                )
            raise LadderError(f"{strategy_id} уже на верхней ступени {rung}")
        if row.status == Status.DEGRADED:
            raise LadderError(f"{strategy_id}: сначала устраните пробой стопа")
        m = self._metrics(strategy_id, rung)
        if m is None:
            raise LadderError(f"{strategy_id}: нет метрик для допуска на {rung}")
        result = self._admission(row, m, rung)
        if result.status != "passed":
            detail = "; ".join(
                f"{c.name}: {c.detail}" for c in result.criteria if c.passed is not True
            ) or result.status
            raise LadderError(f"{strategy_id}: порог на {rung} не пройден: {detail}")
        # Операторский снимок не может подменить фактический результат допуска.
        snap = dict(snapshot or {})
        snap.update(metrics_snapshot(m, result))
        return self._move(
            row, target, Status.MEASURING, reason or f"поднята вручную с {rung}", by, snap
        )

    def demote(
        self,
        strategy_id: str,
        reason: str,
        *,
        by: By = "system",
        snapshot: dict[str, Any] | None = None,
    ) -> Transition | None:
        row = self._movable(strategy_id)
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
        row = self._movable(strategy_id)
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

    def _admission(self, row: StrategyRow, m: Metrics, rung: Rung) -> ThresholdResult:
        from lab.core.forward import current_observation, rules_spec, virtual_health
        from lab.core.measure.types import Criterion
        from lab.core.registry import Strategy, _fingerprint

        result = self._threshold(m, Branch(row.branch), rung)
        # More search since this estimate changes its multiplicity correction.
        # Never silently use the interval selected under a smaller trial count.
        if rung in (Rung.SEMI, Rung.AUTO):
            from lab.core.measure.research import variant_count
            from lab.db.models import ResearchTrialRow

            trial = self.s.get(ResearchTrialRow, m.research.get("trial_id")) if m.research else None
            if (
                not m.research.get("tracked") or m.research.get("variants") != variant_count(self.s)
                or trial is None or trial.status != "ok" or trial.strategy_id != row.id
                or trial.variant_hash != m.research.get("variant_hash")
            ):
                result = result.model_copy(update={
                    "status": "insufficient",
                    "criteria": [*result.criteria, Criterion(
                        name="research_current", value=None, limit=1, op=">=", passed=None,
                        detail="нужен новый расчёт после изменения числа вариантов поиска",
                    )],
                })
        if rung == Rung.BACKTEST:
            valid = m.rules_revision == _fingerprint(rules_spec(Strategy.from_row(row)))
        else:
            observation = current_observation(self.s, row.id)
            valid = (
                observation is not None and m.forward_verified
                and m.observation_id == observation.id
                and m.rules_revision == observation.revision
                and m.observation_started_at == observation.started_at
                and m.trade_source == ("journal_paper" if rung == Rung.PAPER else "journal_live")
                and m.window_to >= observation.started_at
                and (rung != Rung.PAPER or not virtual_health(self.s, row.id))
            )
        criterion = Criterion(
            name="rules_revision" if rung == Rung.BACKTEST else "forward_evidence",
            value=1 if valid else None, limit=1, op=">=",
            passed=bool(valid),
            detail="нужны метрики текущей версии; для форварда — журнал после начала наблюдения",
        )
        return result.model_copy(update={
            "status": result.status if valid else "insufficient",
            "criteria": [*result.criteria, criterion],
        })

    def _row(self, strategy_id: str) -> StrategyRow:
        row = self.s.get(StrategyRow, strategy_id)
        if row is None:
            raise StrategyNotFound(f"стратегия {strategy_id} не найдена")
        return row

    def _movable(self, strategy_id: str) -> StrategyRow:
        row = self._row(strategy_id)
        if row.status == Status.RETIRED:
            raise LadderError(f"{strategy_id} в статусе retired: ступень не меняется")
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
        if to != Rung.BACKTEST:
            from lab.core.forward import current_observation

            current_observation(self.s, row.id, start=True)
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
            )
            .order_by(MeasurementRow.created_at.desc(), MeasurementRow.id.desc())
            .limit(1)
        )
        row = self.s.scalar(stmt)
        if row is None or row.status != "ok" or not row.metrics_json.get("metrics"):
            return None
        return Metrics.model_validate(row.metrics_json["metrics"])
