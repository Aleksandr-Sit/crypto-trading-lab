"""core.measure.run — один замер = неизменяемый снимок (R11.2, R11.5, R11.6, R12.3, R10).

run(strategy_id, mode, window, *, strategy, candles | source, benchmark, session, ...)
  → Measurement(status ok|incomplete, data_hash, code_version, metrics, threshold, folds).

Данные: `candles` (готовый список) или `source(instrument, tf, from, to)` (сигнатура Feed.candles).
Ошибка источника или разрыв ряда → status `incomplete` с причиной, цифр нет.
Повтор с теми же данными/кодом/параметрами возвращает уже сохранённый снимок (cached=True).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.config import ThresholdConfig
from lab.contracts import Branch, Candle, MeasureMode, Rung, StrategyManifest
from lab.core.costs import CostModel, Depth, default_model
from lab.core.measure.metrics import metrics as compute_metrics
from lab.core.measure.metrics import threshold as compute_threshold
from lab.core.measure.simulator import PaperEngine, simulate
from lab.core.measure.types import (
    ClosedTrade,
    FoldResult,
    IncompleteData,
    Measurement,
    Metrics,
    ThresholdResult,
)
from lab.db.models import MeasurementRow

Source = Callable[[str, str, datetime, datetime], Sequence[Candle]]
_ROOT = Path(__file__).resolve().parents[4]


# -- манифест веток: что можно бэктестить и почём замер (R10, R12.2) ------------------


class MeasurePlan(BaseModel):
    branch: Branch
    can_backtest: bool
    measure_cost: int  # условные единицы: 1 — бэктест на свечах, выше — только форвард
    reason: str


_PLANS: dict[Branch, tuple[bool, int, str]] = {
    Branch.CEX_SPOT: (True, 1, "свечи площадки доступны — бэктест первым"),
    Branch.CEX_PERP: (True, 1, "свечи и история фандинга доступны"),
    Branch.DEX_PERP: (True, 2, "свечи Hyperliquid доступны, глубина — оценка"),
    Branch.RH: (True, 2, "свечи есть, спред брокера — оценка"),
    Branch.COPY: (
        False,
        3,
        "нет честной истории: сделки лидера видны с задержкой — только форвард",
    ),
    Branch.PREDICTION: (False, 3, "исходы рынков не воспроизводимы задним числом — форвард"),
    Branch.MEME: (False, 4, "ранние стадии: ликвидность и MEV не восстановить — только форвард"),
    Branch.NFT: (False, 4, "минты и флоры не имеют честной истории — только форвард"),
}


_BRANCH_PREFIXES: tuple[Branch, ...] = tuple(
    sorted(Branch, key=lambda b: len(b.value), reverse=True)
)


def branch_of_strategy_id(strategy_id: str) -> Branch | None:
    """Ветка из читаемого id `<ветка>-<источник>-<слаг>` (решение §18): самое длинное
    совпадение по списку веток, чтобы `cex-spot-…` не превратилось в `cex`."""
    for branch in _BRANCH_PREFIXES:
        if strategy_id == branch.value or strategy_id.startswith(f"{branch.value}-"):
            return branch
    return None


def measure_plan(manifest: StrategyManifest | Branch | str) -> MeasurePlan:
    branch = manifest.branch if isinstance(manifest, StrategyManifest) else Branch(manifest)
    can, cost, reason = _PLANS[branch]
    if isinstance(manifest, StrategyManifest) and not manifest.can_backtest:
        can, reason = False, "манифест стратегии: can_backtest=false"
        cost = max(cost, 3)
    return MeasurePlan(branch=branch, can_backtest=can, measure_cost=cost, reason=reason)


# -- снимок -------------------------------------------------------------------------


def code_version() -> str:
    env = os.environ.get("LAB_CODE_VERSION")
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        sha = out.stdout.strip()
        if out.returncode == 0 and sha:
            return sha
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def data_hash(candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None) -> str:
    h = hashlib.sha256()
    for tag, rows in (("data", candles), ("bench", benchmark or ())):
        h.update(tag.encode())
        for c in rows:
            h.update(
                f"{c.instrument}|{c.tf}|{c.ts.isoformat()}|{c.open}|{c.high}|{c.low}|{c.close}|"
                f"{c.volume}\n".encode()
            )
    return h.hexdigest()


def _params_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()


def walk_forward_windows(
    start: datetime,
    end: datetime,
    *,
    in_sample: timedelta,
    out_of_sample: timedelta,
    step: timedelta | None = None,
) -> list[FoldResult]:
    """Скользящие окна: [is_from, is_to) обучение, [oos_from, oos_to) проверка; шаг = oos."""
    step = step or out_of_sample
    folds: list[FoldResult] = []
    is_from = start
    while is_from + in_sample + out_of_sample <= end:
        is_to = is_from + in_sample
        folds.append(
            FoldResult(is_from=is_from, is_to=is_to, oos_from=is_to, oos_to=is_to + out_of_sample)
        )
        is_from += step
    return folds


def _slice(candles: Sequence[Candle], a: datetime, b: datetime) -> list[Candle]:
    return [c for c in candles if a <= c.ts < b]


def _resolve(strategy):
    if hasattr(strategy, "manifest") and hasattr(strategy, "on_bar"):
        return strategy, None
    if callable(strategy):
        return strategy(), strategy
    raise TypeError("strategy: нужен объект Strategy или фабрика, возвращающая Strategy")


def _row_to_measurement(row: MeasurementRow, cached: bool) -> Measurement:
    payload = dict(row.metrics_json)
    return Measurement(
        id=row.id,
        strategy_id=row.strategy_id,
        mode=MeasureMode(row.mode),
        window_from=row.window_from,
        window_to=row.window_to,
        data_hash=row.data_hash,
        code_version=row.code_version,
        costs_version=payload.get("costs_version", ""),
        params=payload.get("params", {}),
        status=row.status,  # type: ignore[arg-type]
        reason=payload.get("reason", ""),
        metrics=Metrics.model_validate(payload["metrics"]) if payload.get("metrics") else None,
        threshold=ThresholdResult.model_validate(row.threshold_json)
        if row.threshold_json
        else None,
        folds=[FoldResult.model_validate(f) for f in payload.get("folds", [])],
        trades=[ClosedTrade.model_validate(t) for t in payload.get("trades", [])],
        cached=cached,
        created_at=row.created_at,
    )


def _persist(session: Session, m: Measurement) -> Measurement:
    payload = {
        "costs_version": m.costs_version,
        "params": m.params,
        "reason": m.reason,
        "metrics": m.metrics.model_dump(mode="json") if m.metrics else None,
        "folds": [f.model_dump(mode="json") for f in m.folds],
        "trades": [t.model_dump(mode="json") for t in m.trades],
    }
    row = MeasurementRow(
        strategy_id=m.strategy_id,
        mode=m.mode.value,
        window_from=m.window_from,
        window_to=m.window_to,
        data_hash=m.data_hash,
        code_version=m.code_version,
        metrics_json=payload,
        threshold_json=m.threshold.model_dump(mode="json") if m.threshold else {},
        status=m.status,
    )
    session.add(row)
    session.flush()
    return m.model_copy(update={"id": row.id, "created_at": row.created_at})


def run(
    strategy_id: str,
    mode: MeasureMode | str,
    window: tuple[datetime, datetime],
    *,
    strategy=None,
    candles: Sequence[Candle] | None = None,
    source: Source | None = None,
    benchmark: Sequence[Candle] | Decimal | None = None,
    trades: Sequence[ClosedTrade] | None = None,
    branch: Branch | str | None = None,
    session: Session | None = None,
    costs: CostModel | None = None,
    capital: Decimal = Decimal(10_000),
    walk_forward: tuple[timedelta, timedelta] | None = None,
    params: dict[str, Any] | None = None,
    threshold_config: ThresholdConfig | None = None,
    rung: Rung | str | None = None,
    depth: Depth | None = None,
    funding_rate: Decimal = Decimal("0.0001"),
    seed: int = 0,
    extra_metrics: dict[str, object] | None = None,
) -> Measurement:
    mode = MeasureMode(mode)
    model = costs or default_model()
    window_from, window_to = window
    params = dict(params or {})
    params.setdefault("capital", str(capital))
    params.setdefault("seed", seed)
    if walk_forward:
        params.setdefault("walk_forward", [str(walk_forward[0]), str(walk_forward[1])])
    base = dict(
        strategy_id=strategy_id,
        mode=mode,
        window_from=window_from,
        window_to=window_to,
        code_version=code_version(),
        costs_version=model.version,
        params=params,
    )

    # -- форвард/микро: сделки уже есть (из журнала), симулятор не нужен -----------
    if mode in (MeasureMode.MICRO, MeasureMode.FORWARD) and trades is not None:
        bench_rows = benchmark if isinstance(benchmark, Sequence) else None
        dh = (
            hashlib.sha256(
                json.dumps([t.model_dump(mode="json") for t in trades], sort_keys=True).encode()
            ).hexdigest()
            if bench_rows is None
            else data_hash([], bench_rows)
        )
        return _finish(
            base,
            dh,
            list(trades),
            benchmark,
            capital,
            window,
            threshold_config,
            rung,
            seed,
            extra_metrics,
            session,
            strategy,
            branch=branch,
        )

    if strategy is None:
        raise ValueError("нужна стратегия (объект или фабрика)")
    inst, factory = _resolve(strategy)
    manifest: StrategyManifest = inst.manifest
    tf = manifest.timeframe or "1h"
    instrument = manifest.instruments[0]

    # -- данные: готовые свечи или источник; сбой → incomplete -----------------------
    try:
        if candles is None:
            if source is None:
                raise IncompleteData("нет данных: не переданы ни candles, ни source")
            candles = list(source(instrument, tf, window_from, window_to))
        candles = _slice(candles, window_from, window_to)
        if not candles:
            raise IncompleteData("нет свечей в окне")
        bench_rows = benchmark if isinstance(benchmark, Sequence) else None
        dh = data_hash(candles, bench_rows)
        cached = _lookup(session, base, dh)
        if cached is not None:
            return cached

        def engine() -> PaperEngine:
            return PaperEngine(
                venue=manifest.venue,
                instrument=instrument,
                tf=tf,
                branch=manifest.branch,
                costs=model,
                depth=depth,
                funding_rate=funding_rate,
            )

        folds: list[FoldResult] = []
        if walk_forward:
            if factory is None:
                raise ValueError(
                    "walk-forward требует фабрику стратегии (свежий экземпляр на окно)"
                )
            for f in walk_forward_windows(
                window_from, window_to, in_sample=walk_forward[0], out_of_sample=walk_forward[1]
            ):
                is_r = simulate(factory(), _slice(candles, f.is_from, f.is_to), engine=engine())
                oos_r = simulate(factory(), _slice(candles, f.oos_from, f.oos_to), engine=engine())
                folds.append(
                    f.model_copy(
                        update={
                            "in_sample": compute_metrics(
                                is_r.trades,
                                None,
                                capital=capital,
                                window=(f.is_from, f.is_to),
                                config=threshold_config,
                                seed=seed,
                            ),
                            "out_of_sample": compute_metrics(
                                oos_r.trades,
                                None,
                                capital=capital,
                                window=(f.oos_from, f.oos_to),
                                config=threshold_config,
                                seed=seed,
                            ),
                        }
                    )
                )
        result = simulate(inst, candles, engine=engine())
    except IncompleteData as err:
        return _incomplete(base, session, str(err), candles)
    except (OSError, ConnectionError, TimeoutError) as err:
        return _incomplete(base, session, f"источник данных упал: {err}", candles)

    return _finish(
        base,
        dh,
        result.trades,
        benchmark,
        capital,
        window,
        threshold_config,
        rung,
        seed,
        extra_metrics,
        session,
        inst,
        folds,
        branch=branch,
    )


def _branch(strategy_id: str, inst, branch: Branch | str | None) -> Branch:
    """Ветка замера: явный аргумент → манифест стратегии → префикс id (решение §18)."""
    if branch is not None:
        return Branch(branch)
    manifest = getattr(inst, "manifest", None)
    if manifest is not None:
        return manifest.branch
    from_id = branch_of_strategy_id(strategy_id)
    if from_id is None:
        raise ValueError(
            f"не удалось определить ветку по id {strategy_id!r}: передай branch= явно "
            f"(ожидается id вида <ветка>-<источник>-<слаг>)"
        )
    return from_id


def _lookup(session: Session | None, base: dict[str, Any], dh: str) -> Measurement | None:
    if session is None:
        return None
    rows = session.scalars(
        select(MeasurementRow).where(
            MeasurementRow.strategy_id == base["strategy_id"],
            MeasurementRow.mode == base["mode"].value,
            MeasurementRow.window_from == base["window_from"],
            MeasurementRow.window_to == base["window_to"],
            MeasurementRow.data_hash == dh,
            MeasurementRow.code_version == base["code_version"],
            MeasurementRow.status == "ok",
        )
    ).all()
    wanted = _params_hash(base["params"])
    for row in rows:
        payload = row.metrics_json
        if (
            payload.get("costs_version") == base["costs_version"]
            and _params_hash(payload.get("params", {})) == wanted
        ):
            return _row_to_measurement(row, cached=True)
    return None


def _incomplete(base, session, reason: str, candles) -> Measurement:
    m = Measurement(
        id=None, data_hash=data_hash(candles or []), status="incomplete", reason=reason, **base
    )
    return _persist(session, m) if session is not None else m


def _finish(
    base,
    dh,
    trades,
    benchmark,
    capital,
    window,
    threshold_config,
    rung,
    seed,
    extra_metrics,
    session,
    inst,
    folds=(),
    branch: Branch | str | None = None,
) -> Measurement:
    resolved = _branch(base["strategy_id"], inst, branch)
    m_ = compute_metrics(
        trades,
        benchmark,
        capital=capital,
        window=window,
        config=threshold_config,
        seed=seed,
        extra=extra_metrics,
    )
    t_ = compute_threshold(m_, resolved, rung=rung, config=threshold_config)
    m = Measurement(
        id=None,
        data_hash=dh,
        status="ok",
        metrics=m_,
        threshold=t_,
        folds=list(folds),
        trades=list(trades),
        **base,
    )
    return _persist(session, m) if session is not None else m


__all__ = [
    "MeasurePlan",
    "Source",
    "branch_of_strategy_id",
    "code_version",
    "data_hash",
    "measure_plan",
    "run",
    "walk_forward_windows",
]
