"""Метрики ветки `prediction`: Brier и доходность к резолюции (История 83).

Рынок предсказаний измеряется в вероятностях: цена контракта — это заявленная вероятность,
а исход бинарен. Brier = среднее (p − исход)²: 0 — идеальный прогноз, 0.25 — «монетка».
Доходность к резолюции = (выплата − вложено) / вложено, потому что контракт гасится по 1 или 0,
а не продаётся по «цене».

Обе метрики приводятся к общим P&L/DD через `resolution_trades`: те же ставки становятся
закрытыми сделками (вход по вероятности, выход по исходу), и `core.measure.metrics` считает по
ним `net_pnl`, `max_dd_pct` и порог — ветка не живёт в своей системе координат.
Имена метрик — из словаря T02 (`brier`, `resolution_return`), они и передаются в
`core.measure.run(..., extra_metrics=...)`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from lab.contracts import Costs
from lab.core.measure.types import ClosedTrade

ONE = Decimal(1)
ZERO = Decimal(0)


@dataclass(frozen=True)
class ResolvedBet:
    """Ставка, дожившая до резолюции рынка: вероятность входа и фактический исход."""

    strategy_id: str
    market: str
    token_id: str
    outcome: str
    probability: Decimal
    qty: Decimal
    resolved_yes: bool
    opened_at: datetime
    resolved_at: datetime
    costs: Costs | None = None

    @property
    def result(self) -> Decimal:
        """1, если купленный исход выиграл, иначе 0 — цена гашения контракта."""
        return ONE if self.resolved_yes else ZERO

    @property
    def cost_usd(self) -> Decimal:
        return self.qty * self.probability

    @property
    def payout_usd(self) -> Decimal:
        return self.qty * self.result


def brier_score(bets: Sequence[ResolvedBet]) -> Decimal | None:
    """Средний квадрат ошибки вероятности. `None`, когда мерить не на чем."""
    if not bets:
        return None
    total = sum(((b.probability - b.result) ** 2 for b in bets), ZERO)
    return total / Decimal(len(bets))


def resolution_return(bets: Sequence[ResolvedBet]) -> Decimal | None:
    """Доходность к резолюции в процентах от вложенного (издержки входят, если заданы)."""
    invested = sum((b.cost_usd for b in bets), ZERO)
    if invested <= 0:
        return None
    costs = sum(((b.costs.total if b.costs else ZERO) for b in bets), ZERO)
    payout = sum((b.payout_usd for b in bets), ZERO)
    return (payout - invested - costs) / invested * Decimal(100)


def resolution_trades(bets: Sequence[ResolvedBet]) -> list[ClosedTrade]:
    """Ставки как закрытые сделки: вход по вероятности, выход по исходу (1 или 0)."""
    return [
        ClosedTrade(
            instrument=b.token_id,
            side="long",
            qty=b.qty,
            entry_price=b.probability,
            exit_price=b.result,
            opened_at=b.opened_at,
            closed_at=b.resolved_at,
            pnl_gross=(b.result - b.probability) * b.qty,
            costs=b.costs or Costs(),
        )
        for b in bets
    ]


def measure_extra(bets: Sequence[ResolvedBet]) -> dict[str, object]:
    """Ветко-специфичные метрики для `core.measure.run(..., extra_metrics=...)`."""
    extra: dict[str, object] = {}
    brier = brier_score(bets)
    if brier is not None:
        extra["brier"] = brier
    ret = resolution_return(bets)
    if ret is not None:
        extra["resolution_return"] = ret
    return extra


__all__ = [
    "ResolvedBet",
    "brier_score",
    "measure_extra",
    "resolution_return",
    "resolution_trades",
]
