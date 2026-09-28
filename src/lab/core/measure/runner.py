"""core.measure.run — один замер = неизменяемый снимок (R11.2, R11.5, R11.6, R12.3, R10).

run(strategy_id, mode, window, *, strategy, candles | source, benchmark, session, ...)
  → Measurement(status ok|incomplete, data_hash, code_version, metrics, threshold, folds).

Данные: `candles` (готовый список) или `source(instrument, tf, from, to)` (сигнатура Feed.candles).
Ошибка источника или разрыв ряда → status `incomplete` с причиной, цифр нет.
Повтор с теми же данными/кодом/параметрами возвращает уже сохранённый снимок (cached=True).
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import subprocess
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from lab.config import ThresholdConfig
from lab.contracts import Branch, Candle, MeasureMode, Rung, StrategyManifest
from lab.contracts.allocation import is_allocation
from lab.contracts.timeframes import parse_tf
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


def _hash_row(h: Any, c: Candle) -> None:
    h.update(
        f"{c.instrument}|{c.tf}|{c.ts.isoformat()}|{c.open}|{c.high}|{c.low}|{c.close}|"
        f"{c.volume}\n".encode()
    )


class DataHasher:
    """Отпечаток данных замера, считаемый ПО ХОДУ чтения.

    Порядок байтов тот же, что в `data_hash`: тег `data`, все свечи, тег `bench`, бенчмарк, —
    поэтому поток и готовый список дают одинаковый отпечаток. Нужен потому, что ряд больше
    в память не помещается: хешировать его можно только по мере поступления.
    """

    def __init__(self) -> None:
        self._h = hashlib.sha256()
        self._h.update(b"data")
        self._closed = False

    def add(self, candle: Candle) -> None:
        _hash_row(self._h, candle)

    def wrap(self, candles: Iterable[Candle]) -> Iterator[Candle]:
        """Пропускает поток через себя, попутно хешируя каждую свечу."""
        for c in candles:
            self.add(c)
            yield c

    def digest(
        self,
        benchmark: Sequence[Candle] | None = None,
        funding: dict[str, dict[datetime, Decimal]] | None = None,
        positioning: dict[str, dict[datetime, dict[str, Decimal]]] | None = None,
    ) -> str:
        """Отпечаток всех данных замера: свечи, бенчмарк, ставки фандинга и метрики.

        Всё, ЧЕМ КОРМЯТ стратегию, обязано входить в отпечаток. Иначе замер на реальных
        данных и замер без них дают ОДИН хеш, и вместо нового расчёта вернётся сохранённый
        снимок — с чужими цифрами. Ставки эту ошибку уже ловили; метрики позиционирования
        добавлены сюда по той же причине. Для спота оба словаря пусты, отпечаток не меняется.
        """
        if not self._closed:
            self._h.update(b"bench")
            for c in benchmark or ():
                _hash_row(self._h, c)
            self._h.update(b"funding")
            for instrument in sorted(funding or {}):
                for ts in sorted(funding[instrument]):
                    row = funding[instrument][ts]
                    self._h.update(f"{instrument}|{ts.isoformat()}|{row}\n".encode())
            self._h.update(b"positioning")
            for instrument in sorted(positioning or {}):
                for ts in sorted(positioning[instrument]):
                    values = positioning[instrument][ts]
                    fields = "|".join(f"{k}={values[k]}" for k in sorted(values))
                    self._h.update(f"{instrument}|{ts.isoformat()}|{fields}\n".encode())
            self._closed = True
        return self._h.hexdigest()


def data_hash(candles: Sequence[Candle], benchmark: Sequence[Candle] | None = None) -> str:
    hasher = DataHasher()
    for c in candles:
        hasher.add(c)
    return hasher.digest(benchmark)


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


CHUNK_BARS = 20_000
"""Сколько свечей держать в памяти за раз при чтении из источника.

20 000 минутных баров — это две недели ряда и десятки мегабайт; окно целиком (год минуток —
полмиллиона объектов Candle) занимает больше гигабайта и на сервере ловит OOM-killer.
"""


MIN_CHUNK_BARS = 250
"""Нижний предел куска на инструмент.

`heapq.merge` заводит ВСЕ потоки сразу, и каждый тут же тянет свой первый кусок: общая
память — это предел, умноженный на число рядов. Прежние 2 000 хороши для полусотни
инструментов и смертельны для вселенной кросс-моментума: 647 × 2 000 — это 1.3 млн свечей,
и замер получил SIGKILL по `mem_limit` контейнера (код 137, вывода нет). 250 даёт около
160 тысяч свечей в пике ценой более частых запросов к хранилищу.
"""


def chunk_for(instruments: int) -> int:
    """Сколько баров тянуть за раз на ОДИН ряд, чтобы в сумме не выйти за бюджет."""
    return max(MIN_CHUNK_BARS, CHUNK_BARS // max(1, instruments))


def _merged(
    candles: Sequence[Candle] | None,
    source: Source | None,
    instruments: Sequence[str],
    tf: str,
    a: datetime,
    b: datetime,
) -> Iterator[Candle]:
    """Свечи нескольких инструментов, слитые по времени.

    Портфельным стратегиям (кроссмоментум, базис, фандинг-арбитраж) нужны несколько рядов
    ОДНОВРЕМЕННО, иначе их нечем мерить. Ряды читаются кусками параллельно и сливаются
    по времени: в памяти лежит по куску на инструмент, а не всё окно.
    """
    if len(instruments) == 1:
        yield from _stream(candles, source, instruments[0], tf, a, b)
        return
    # Кусок делится между рядами: пятьдесят инструментов по 20 тысяч баров — это снова
    # миллион свечей в памяти, ради чего всё и переписывалось на потоки.
    per_instrument = chunk_for(len(instruments))
    streams = [
        _stream(candles, source, name, tf, a, b, chunk_bars=per_instrument, match=True)
        for name in instruments
    ]
    yield from heapq.merge(*streams, key=lambda c: c.ts)


def _stream(
    candles: Sequence[Candle] | None,
    source: Source | None,
    instrument: str,
    tf: str,
    a: datetime,
    b: datetime,
    chunk_bars: int | None = None,
    match: bool = False,
) -> Iterator[Candle]:
    """Свечи окна [a, b) потоком: из готового списка либо из источника кусками.

    Источник спрашивается по отрезкам, а не на всё окно сразу; каждый ответ отфильтрован
    по своему отрезку, поэтому свеча на стыке не задваивается, даже если источник считает
    правую границу включительно.
    """
    if candles is not None:
        # `match` — когда инструментов несколько: готовый список общий на всех, и без
        # отбора по имени один и тот же ряд ушёл бы каждому движку. С одним инструментом
        # не фильтруем: поток и есть он, как бы ряд ни назывался (синтетика в тестах).
        yield from (
            c
            for c in candles
            if a <= c.ts < b and (not match or c.instrument == instrument)
        )
        return
    if source is None:
        raise IncompleteData("нет данных: не переданы ни candles, ни source")
    span = parse_tf(tf) * (chunk_bars or CHUNK_BARS)
    cursor = a
    while cursor < b:
        upto = min(cursor + span, b)
        for c in source(instrument, tf, cursor, upto):
            if cursor <= c.ts < upto:
                yield c
        cursor = upto


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


def history(
    session: Session,
    strategy_id: str,
    *,
    mode: MeasureMode | str | None = None,
    limit: int = 5,
) -> list[Measurement]:
    """Сохранённые снимки стратегии, свежие первыми (`lab measure show`, веб-карточка)."""
    stmt = select(MeasurementRow).where(MeasurementRow.strategy_id == strategy_id)
    if mode is not None:
        stmt = stmt.where(MeasurementRow.mode == MeasureMode(mode).value)
    stmt = stmt.order_by(MeasurementRow.created_at.desc(), MeasurementRow.id.desc()).limit(limit)
    return [_row_to_measurement(row, cached=True) for row in session.scalars(stmt)]


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
    funding_history: dict[str, dict[datetime, Decimal]] | None = None,
    # Метрики позиционирования по инструментам: «инструмент → момент → показатели».
    # Отдельно от ставок намеренно: связь изменения открытого интереса со ставкой −0.03,
    # это разные данные, и класть их в один словарь значило бы потерять второй.
    positioning_history: dict[str, dict[datetime, dict[str, Decimal]]] | None = None,
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
    instruments = list(manifest.instruments)
    # Плечо ноги с залогом. Нет в параметрах — единица: позиция обеспечена целиком, и это
    # самое мягкое из честных допущений. Ноль или мусор трактуем так же, а не падаем:
    # замер не место для валидации карточек.
    try:
        leverage = Decimal(str(manifest.params.get("leverage", 1) or 1))
    except (ArithmeticError, ValueError):
        leverage = Decimal(1)
    if leverage <= 0:
        leverage = Decimal(1)
    # Режим маржи. По умолчанию ИЗОЛИРОВАННАЯ — так на бирже и стоит, пока её не
    # переключишь, и она строже: спот-нога хеджа не считается обеспечением фьючерсной.
    # `cross` — общий счёт: прибыль одной ноги держит убыток другой.
    cross_margin = str(manifest.params.get("margin_mode", "isolated")).lower() == "cross"
    # Правило размещения: стоп по просадке от вершины с открытой позицией — как в бою.
    allocation = is_allocation(manifest.params)

    # -- данные: готовые свечи или источник; сбой → incomplete -----------------------
    bench_rows = benchmark if isinstance(benchmark, Sequence) else None
    hasher = DataHasher()
    try:
        if candles is None and source is None:
            raise IncompleteData("нет данных: не переданы ни candles, ни source")

        def engines() -> dict[str, PaperEngine]:
            """Свой движок на каждый инструмент: позиции, филлы и фандинг у них разные,
            а капитал и стоп — общие (их считает симулятор по всем сделкам вместе)."""
            return {
                name: PaperEngine(
                    venue=manifest.venue,
                    instrument=name,
                    tf=tf,
                    branch=manifest.branch,
                    costs=model,
                    depth=depth,
                    funding_rate=funding_rate,
                    # Реальные ставки, если их собрали: у нейтральных стратегий весь доход
                    # именно в фандинге, и константа вместо истории мерила бы выдуманное.
                    funding_rates=(funding_history or {}).get(name),
                    positioning=(positioning_history or {}).get(name),
                    # Двоеточие в имени ccxt — признак перпа: у связки «спот + перп» обе
                    # ноги в перп-ветке, но фандинг платит только перповая. Ветка при этом
                    # ни при чём: спотовая стратегия может держать перп как ИСТОЧНИК
                    # СИГНАЛА (кросс-моментум смотрит на ставку, чтобы понять фазу рынка),
                    # и без этого события фандинга до неё не доезжали вовсе.
                    is_perp=":" in name,
                    # Плечо — только у ног с залогом (тем же двоеточием отличаются перпы
                    # и срочные контракты от спота). У спот-ноги залога нет, и ликвидировать
                    # её нельзя: это ключевая асимметрия хеджа «спот + шорт фьючерса».
                    leverage=leverage if ":" in name else None,
                    # Замер исполнения объектами не хранит: их читает только он сам,
                    # а сеточная стратегия на минутках набирает их сотнями тысяч.
                    keep_fills=False,
                )
                for name in instruments
            }

        folds: list[FoldResult] = []
        if walk_forward:
            if factory is None:
                raise ValueError(
                    "walk-forward требует фабрику стратегии (свежий экземпляр на окно)"
                )
            for f in walk_forward_windows(
                window_from, window_to, in_sample=walk_forward[0], out_of_sample=walk_forward[1]
            ):
                is_r = simulate(
                    factory(),
                    _merged(candles, source, instruments, tf, f.is_from, f.is_to),
                    engines=engines(),
                    stop=manifest.stop,
                    capital=capital,
                    allocation=allocation,
                )
                oos_r = simulate(
                    factory(),
                    _merged(candles, source, instruments, tf, f.oos_from, f.oos_to),
                    engines=engines(),
                    stop=manifest.stop,
                    capital=capital,
                    allocation=allocation,
                )
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
        # Отпечаток данных считается по ходу единственного прохода: второй раз ряд не читаем,
        # иначе живой источник опрашивался бы дважды. Поэтому и сохранённый снимок ищется
        # ПОСЛЕ прогона — раньше отпечатка просто нет.
        # Стоп стратегии из манифеста действует и в симуляции: иначе бэктест показывает
        # то, чего живая система никогда не сделает — она бы остановилась на первом пробое
        # (`RiskEngine` → `strategy_stop_dd|daily`, `StopWatch` → degraded).
        result = simulate(
            inst,
            hasher.wrap(_merged(candles, source, instruments, tf, window_from, window_to)),
            engines=engines(),
            stop=manifest.stop,
            capital=capital,
            allocation=allocation,
            # Дыра в ОДНОМ ряду не отменяет замер портфеля: остановка торгов отдельным
            # альтом — обычное дело, а вселенная кросс-моментума состоит из сотен пар,
            # и хоть одна дыра там есть всегда. Пропуски считаются и уезжают в снимок.
            allow_gaps=len(instruments) > 1,
            cross_margin=cross_margin,
        )
        dh = hasher.digest(bench_rows, funding_history, positioning_history)
        cached = _lookup(session, base, dh)
        if cached is not None:
            return cached
        # Переоценка по рынку — всегда: без неё незакрытая на конец окна позиция выпадала
        # из итога, а просадка считалась только по закрытым сделкам (ревизия 12.09.2026).
        extra_metrics = {
            **(extra_metrics or {}),
            "unrealized_pnl": result.unrealized_end,
            "mtm_max_dd_pct": result.mtm_max_dd_pct,
            # Средний занятый залог: без него стратегия, работающая десятой частью счёта,
            # сравнивалась с депозитом на ВЕСЬ счёт, хотя её простаивающие деньги лежали
            # бы в тех же казначейских бумагах.
            "avg_margin": result.avg_margin,
        }
        if result.reasons:
            extra_metrics = {**(extra_metrics or {}), "reasons": dict(result.reasons)}
        if result.liquidations:
            # Ликвидация — не «одна из метрик», а ответ на вопрос, дожил ли счёт. Без неё
            # доход правил читался бы как доход стратегии, хотя биржа закрыла бы ногу.
            first_at, first_what = result.liquidations[0][1], result.liquidations[0][0]
            extra_metrics = {
                **(extra_metrics or {}),
                "liquidations": {
                    "count": len(result.liquidations),
                    "first_at": first_at.isoformat(),
                    "first_instrument": first_what,
                },
            }
        if result.gaps:
            # Сколько баров недосчитались и по скольким рядам: замер прошёл, но данные
            # были дырявые, и снимок обязан это признавать.
            extra_metrics = {
                **(extra_metrics or {}),
                "data_gaps": {
                    "instruments": len(result.gaps),
                    "bars": sum(result.gaps.values()),
                },
            }
        if result.stopped_at is not None:
            # Без этой пометки снимок читается неверно: цифры обрываются на пробое стопа,
            # а по метрикам это выглядит как «стратегия просто перестала торговать».
            extra_metrics = dict(extra_metrics or {})
            extra_metrics["stopped_at"] = result.stopped_at
            extra_metrics["stop_rule"] = result.stop_rule
            extra_metrics["blocked_signals"] = result.blocked_signals
    except IncompleteData as err:
        return _incomplete(
            base,
            session,
            str(err),
            hasher.digest(bench_rows, funding_history, positioning_history),
        )
    except (OSError, ConnectionError, TimeoutError) as err:
        digest = hasher.digest(bench_rows, funding_history, positioning_history)
        return _incomplete(base, session, f"источник данных упал: {err}", digest)

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


def _incomplete(base, session, reason: str, digest: str) -> Measurement:
    """Незавершённый замер: цифр нет, но отпечаток прочитанных данных сохраняем —
    по нему видно, на чём именно оборвалось."""
    m = Measurement(id=None, data_hash=digest, status="incomplete", reason=reason, **base)
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
