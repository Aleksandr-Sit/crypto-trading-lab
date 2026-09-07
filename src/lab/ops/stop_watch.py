"""Связка «пробой стопа → breach» (G04, уточнение к таску 03).

`core.risk.check` только отвечает `Deny(strategy_stop_daily|strategy_stop_dd)`; перевести
стратегию в `degraded` и отменить её ордера должен тот, кто ордер подавал. `StopWatch.guard`
оборачивает `check`: при таком отказе на **открывающем** ордере вызывает `ladder.breach()`
(один раз на стратегию), на **закрывающем** (`reduce_only`) — поднимает `RiskCoreError`:
закрыть позицию при пробитом стопе нельзя не должно, это дефект ядра, а не отказ.

`sweep(probes)` — задание планировщика `stop_watch`: прогоняет пробные намерения по
активным стратегиям, чтобы пробой заметили, не дожидаясь следующего сигнала.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from lab.contracts import OrderIntent
from lab.core.risk import Deny, Verdict

log = logging.getLogger(__name__)

STOP_RULE_PREFIX = "strategy_stop_"


class RiskCoreError(RuntimeError):
    """Риск-ядро отказало закрывающему ордеру по стопу стратегии — дефект, не отказ."""


class RiskLike(Protocol):
    def check(self, intent: OrderIntent) -> Verdict: ...


class LadderLike(Protocol):
    def breach(self, strategy_id: str, reason: str, snapshot: dict[str, Any] | None = None): ...


def is_stop_deny(verdict: Verdict) -> bool:
    return isinstance(verdict, Deny) and verdict.rule.startswith(STOP_RULE_PREFIX)


class StopWatch:
    def __init__(
        self,
        risk: RiskLike,
        ladder: LadderLike,
        *,
        on_breach: Callable[[str, Any], None] | None = None,
    ) -> None:
        self.risk = risk
        self.ladder = ladder
        self._on_breach = on_breach
        self._breached: list[str] = []

    def guard(self, intent: OrderIntent) -> Verdict:
        """`risk.check` + связка со стопом. Возвращает вердикт как есть."""
        verdict = self.risk.check(intent)
        if not is_stop_deny(verdict):
            return verdict
        assert isinstance(verdict, Deny)
        if intent.reduce_only:
            raise RiskCoreError(
                f"{intent.strategy_id}: закрывающий ордер отклонён стопом "
                f"({verdict.rule}: {verdict.reason}) — дефект риск-ядра"
            )
        self._breach(intent, verdict)
        return verdict

    def _breach(self, intent: OrderIntent, verdict: Deny) -> None:
        if intent.strategy_id in self._breached:
            return
        snapshot = {"rule": verdict.rule, "intent": intent.model_dump(mode="json")}
        transition = self.ladder.breach(intent.strategy_id, verdict.reason, snapshot)
        self._breached.append(intent.strategy_id)
        log.warning("Стратегия %s: %s → degraded", intent.strategy_id, verdict.reason)
        if self._on_breach is not None:
            self._on_breach(intent.strategy_id, transition)

    def breached(self) -> list[str]:
        return list(self._breached)

    def forget(self, strategy_id: str) -> None:
        """После ручного возврата стратегии в строй — снова следить."""
        if strategy_id in self._breached:
            self._breached.remove(strategy_id)

    def sweep(self, probes: Iterable[OrderIntent]) -> list[str]:
        """Задание `stop_watch`: прогнать пробные намерения; вернуть id новых пробоев."""
        hit: list[str] = []
        for probe in probes:
            if probe.strategy_id in self._breached or probe.reduce_only:
                continue
            verdict = self.risk.check(probe)
            if is_stop_deny(verdict):
                assert isinstance(verdict, Deny)
                self._breach(probe, verdict)
                hit.append(probe.strategy_id)
        return hit
