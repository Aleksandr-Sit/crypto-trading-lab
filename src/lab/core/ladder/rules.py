"""Таблица переходов лестницы как данные (решение §7).

`backtest → paper → micro → signal → semi → auto`. Вверх — по порогу, кроме `semi → auto`
(только оператор). Вниз — на ступень ниже по провалу порога; пробой стопа — статус `degraded`
без смены ступени. Стратегия без бэктеста (`can_backtest=False`) стартует с `paper`.
"""

from __future__ import annotations

from typing import Literal

from lab.contracts import RUNG_ORDER, MeasureMode, Rung

By = Literal["system", "operator"]

PROMOTE_NEXT: dict[Rung, Rung | None] = {
    rung: (RUNG_ORDER[i + 1] if i + 1 < len(RUNG_ORDER) else None)
    for i, rung in enumerate(RUNG_ORDER)
}
DEMOTE_PREV: dict[Rung, Rung | None] = {
    rung: (RUNG_ORDER[i - 1] if i > 0 else None) for i, rung in enumerate(RUNG_ORDER)
}

# Переходы, которые система не делает сама — только `promote(by="operator")`.
OPERATOR_ONLY: frozenset[tuple[Rung, Rung]] = frozenset({(Rung.SEMI, Rung.AUTO)})

# Ступени, где сигнал ждёт оператора и протухает по ttl (R02, R03, R03.1).
SIGNAL_RUNGS: frozenset[Rung] = frozenset({Rung.SIGNAL, Rung.SEMI})

# Какой режим замера отвечает за порог на ступени.
MEASURE_MODE_FOR_RUNG: dict[Rung, MeasureMode] = {
    Rung.BACKTEST: MeasureMode.BACKTEST,
    Rung.PAPER: MeasureMode.PAPER,
    Rung.MICRO: MeasureMode.MICRO,
    Rung.SIGNAL: MeasureMode.FORWARD,
    Rung.SEMI: MeasureMode.FORWARD,
    Rung.AUTO: MeasureMode.FORWARD,
}


def initial_rung(can_backtest: bool) -> Rung:
    return Rung.BACKTEST if can_backtest else Rung.PAPER


def next_rung(rung: Rung, *, by: By) -> Rung | None:
    """Следующая ступень для `by`, или None, если выше нельзя (верх или нужен оператор)."""
    nxt = PROMOTE_NEXT[rung]
    if nxt is None:
        return None
    if by == "system" and (rung, nxt) in OPERATOR_ONLY:
        return None
    return nxt
