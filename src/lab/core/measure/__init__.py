"""core.measure — замеры, метрики, порог (Границы и швы).

Выставляет: `run(strategy_id, mode, window, ...) -> Measurement`,
`metrics(trades, benchmark, ...) -> Metrics`, `threshold(metrics, branch, ...) -> ThresholdResult`.
Прячет: симулятор (`simulator`), бутстрап, бенчмарк.
"""

from lab.contracts.timeframes import parse_tf
from lab.core.measure.metrics import btc_buy_and_hold_pct, metrics, sample_status, threshold
from lab.core.measure.runner import (
    MeasurePlan,
    Source,
    branch_of_strategy_id,
    code_version,
    data_hash,
    measure_plan,
    run,
    walk_forward_windows,
)
from lab.core.measure.simulator import PaperEngine, SimResult, check_continuity, simulate
from lab.core.measure.types import (
    ClosedTrade,
    CostsBreakdown,
    Criterion,
    FoldResult,
    IncompleteData,
    LookaheadError,
    MeasureError,
    Measurement,
    Metrics,
    NotApplicable,
    SampleStatus,
    ThresholdResult,
)

__all__ = [
    "ClosedTrade",
    "CostsBreakdown",
    "Criterion",
    "FoldResult",
    "IncompleteData",
    "LookaheadError",
    "MeasureError",
    "MeasurePlan",
    "Measurement",
    "Metrics",
    "NotApplicable",
    "PaperEngine",
    "SampleStatus",
    "SimResult",
    "Source",
    "ThresholdResult",
    "branch_of_strategy_id",
    "btc_buy_and_hold_pct",
    "check_continuity",
    "code_version",
    "data_hash",
    "measure_plan",
    "metrics",
    "parse_tf",
    "run",
    "sample_status",
    "simulate",
    "threshold",
    "walk_forward_windows",
]
