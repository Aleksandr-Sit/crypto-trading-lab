"""Отчёт «копия vs лидер» в замере: цена задержки и доля пропущенных сделок.

Метрики отдаются `core.measure.run(..., extra_metrics=...)` — ветко-специфичные метрики
ядро именно так и принимает (таск 02).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

BPS = Decimal(10_000)
CENTS = Decimal("0.01")


@dataclass(frozen=True)
class CopyRecord:
    """Одна сделка лидера и её судьба у нас."""

    leader_tx: str
    side: str
    leader_price: Decimal
    copy_price: Decimal | None = None
    lag_ms: int | None = None
    status: str = "copied"  # copied | missed | skipped
    reason: str = ""
    strategy_id: str = ""


def _avg(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal(0)
    return (sum(values, Decimal(0)) / len(values)).quantize(CENTS, rounding=ROUND_HALF_UP)


def copy_metrics(records: Sequence[CopyRecord]) -> dict[str, Decimal]:
    """`copy_lag_cost` — средняя потеря копии в б.п., `missed_share` — доля непопаданий, %."""
    copied = [r for r in records if r.status == "copied" and r.copy_price is not None]
    costs: list[Decimal] = []
    for record in copied:
        if record.leader_price <= 0:
            continue
        drift = (record.copy_price - record.leader_price) / record.leader_price * BPS
        costs.append(drift if record.side == "buy" else -drift)
    lags = [Decimal(r.lag_ms) for r in copied if r.lag_ms is not None]
    missed = [r for r in records if r.status != "copied"]
    total = len(records)
    return {
        "copy_lag_cost": _avg(costs),
        "copy_lag_ms": _avg(lags),
        "missed_share": (
            (Decimal(len(missed)) / total * 100).quantize(CENTS, rounding=ROUND_HALF_UP)
            if total
            else Decimal(0)
        ),
        "copied": Decimal(len(copied)),
        "leader_trades": Decimal(total),
    }


def measure_extra(records: Sequence[CopyRecord], lag=None) -> dict[str, dict[str, Decimal]]:
    """Отчёт «копия vs лидер» в форме, которую принимает `core.measure`.

    Ядро ждёт `copy_lag_cost` словарём (`Metrics.copy_lag_cost: dict[str, Decimal]`),
    поэтому доля пропущенных сделок и средний лаг едут туда же, а не отдельной метрикой.
    `lag` — `wallets.LagCost` лидера, если он посчитан: тогда в отчёт попадают и
    задержки 5/30/120 с, измеренные по ценам после сделки лидера.
    """
    report = copy_metrics(records)
    values = {
        "lag_cost_bps": report["copy_lag_cost"],
        "lag_ms": report["copy_lag_ms"],
        "missed_share_pct": report["missed_share"],
        "copied": report["copied"],
        "leader_trades": report["leader_trades"],
    }
    if lag is not None:
        values.update({f"delay_{d}s_bps": v for d, v in lag.by_delay_s.items()})
    return {"copy_lag_cost": values}


__all__ = ["CopyRecord", "copy_metrics", "measure_extra"]
