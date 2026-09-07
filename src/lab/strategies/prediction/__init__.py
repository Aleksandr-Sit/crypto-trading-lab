"""Ветка `prediction`: копия позиций топ-кошельков Polymarket и метрики рынков предсказаний."""

from lab.strategies.prediction.metrics import (
    ResolvedBet,
    brier_score,
    measure_extra,
    resolution_return,
    resolution_trades,
)
from lab.strategies.prediction.strategy import (
    POSITION_EVENT,
    SOURCE_KIND,
    VENUE,
    PmCopyStrategy,
    make_pm_copy_strategy,
    pm_copy_manifest,
    slug_of,
)

__all__ = [
    "POSITION_EVENT",
    "SOURCE_KIND",
    "VENUE",
    "PmCopyStrategy",
    "ResolvedBet",
    "brier_score",
    "make_pm_copy_strategy",
    "measure_extra",
    "pm_copy_manifest",
    "resolution_return",
    "resolution_trades",
    "slug_of",
]
