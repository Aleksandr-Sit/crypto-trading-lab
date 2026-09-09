"""Метрики единого словаря (R34i) и порог (В12, R12.1, R11.1, A04).

`metrics(trades, benchmark, ...)` — считает словарь по закрытым сделкам;
`threshold(metrics, branch, ...)` — по каждому критерию значение / порог / пройден.
Бутстрап EV — numpy с фиксированным seed: один и тот же набор сделок даёт один и тот же CI.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

import numpy as np

from lab.config import ThresholdConfig, load_limits, load_threshold
from lab.contracts import Branch, Candle, Rung
from lab.core.measure.types import (
    ClosedTrade,
    CostsBreakdown,
    Criterion,
    Metrics,
    NotApplicable,
    SampleStatus,
    ThresholdResult,
)

_Q = Decimal("0.000000000001")
_DAY = timedelta(days=1)
_NA = {
    "paper_vs_live_gap": NotApplicable(reason="только для ступени micro и выше"),
    "copy_lag_cost": NotApplicable(reason="только для ветки copy"),
    "brier": NotApplicable(reason="только для ветки prediction"),
    "resolution_return": NotApplicable(reason="только для ветки prediction"),
    "mint_success_rate": NotApplicable(reason="только для стратегий nft-mint-*"),
    "mint_cost_failed": NotApplicable(reason="только для стратегий nft-mint-*"),
}

# Факты о ходе прогона: не метрики ветки, но часть снимка — иначе их некуда положить.
_OUTCOME_KEYS = (
    "stopped_at",
    "stop_rule",
    "blocked_signals",
    "benchmark_kind",
    "stability",
    "reasons",
    "data_gaps",
)


def _d(value: float | int | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("не число")
    return Decimal(repr(float(value))).quantize(_Q, rounding=ROUND_HALF_UP)


def btc_buy_and_hold_pct(candles: Sequence[Candle]) -> Decimal:
    """Доходность B&H за окно: последний close / первый open − 1, в %."""
    if not candles:
        raise ValueError("нет свечей бенчмарка")
    return (candles[-1].close / candles[0].open - 1) * 100


def annualized_pct(total_pct: Decimal | None, window: tuple[datetime, datetime]) -> Decimal | None:
    """Доходность за окно → среднегодовая, в %.

    Зачем: проценты за окно между окнами разной длины несравнимы, а на длинной истории
    вычитание процентов теряет смысл вовсе — BTC с 2011 года дал +2 855 000%, и любая
    разница превращается в астрономическое число. Годовые читаются и сопоставляются:
    «+24% в год против +100% в год», а не «−2 852 951».

    None — когда считать нечего: окно короче суток или капитал ушёл в ноль и ниже
    (тогда «среднегодовой» доходности не существует, и выдумывать её нельзя).
    """
    if total_pct is None:
        return None
    days = (window[1] - window[0]).total_seconds() / 86400
    if days < 1:
        return None
    growth = 1 + float(total_pct) / 100
    if growth <= 0:
        return None
    # Считаем через логарифм и отсекаем бессмыслицу: растянуть двухдневные +60% на год —
    # это 10^39 процентов. Такое число не только не читается, оно и в Decimal не влезает
    # (InvalidOperation). Нет годовых — значит сравнение остаётся на процентах за окно.
    rate = math.log(growth) * 365.25 / days
    if abs(rate) > 20:  # e^20 ≈ 4.9e8 % годовых — окно слишком коротко для такой оценки
        return None
    return _d((math.exp(rate) - 1) * 100)


def _vs_benchmark_value(m: Metrics) -> Decimal | None:
    """Насколько стратегия лучше бенчмарка.

    В годовых, если их можно посчитать, иначе — в процентах за окно (короткое окно,
    капитал в нуле). Знак от единицы не зависит: годовые — монотонное преобразование
    доходности за то же окно, поэтому ВЕРДИКТ порога не меняется, меняется читаемость.
    Именно поэтому смена единицы — не решение проблемы «бенчмарк с 2011 недостижим»;
    решает её сравнение по скользящим окнам (`lab measure rolling`).
    """
    return m.vs_benchmark_cagr if m.vs_benchmark_cagr is not None else m.vs_benchmark


def _vs_benchmark_detail(m: Metrics) -> str:
    """Почему сравнения нет — разные причины, и их нельзя смешивать в одну строку."""
    kind = {
        "btc_bh": "BTC купить и держать",
        "btc_dca": "BTC равными докупками",
        "cash": "не делать ничего",
        "none": "без бенчмарка",
    }.get(m.benchmark_kind, m.benchmark_kind or "бенчмарк")
    if m.benchmark_pct is None:
        return f"нет данных бенчмарка за окно ({kind})"
    if m.vs_benchmark_cagr is None:
        return f"процентов за окно против «{kind}» (годовые тут не считаются)"
    return f"годовых против «{kind}»"


def sample_status(n: int, required: int) -> SampleStatus:
    if n >= required:
        return SampleStatus(status="ok", n=n, required=required, detail=f"{n} из {required}")
    return SampleStatus(
        status="insufficient",
        n=n,
        required=required,
        detail=f"недостаточно данных: {n} из {required}",
    )


def _bootstrap_ci(
    pnls: Sequence[Decimal], samples: int, confidence: Decimal, seed: int
) -> tuple[Decimal, Decimal]:
    arr = np.array([float(p) for p in pnls])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(samples, len(arr)))
    means = arr[idx].mean(axis=1)
    alpha = (1 - float(confidence)) / 2
    lo, hi = np.quantile(means, [alpha, 1 - alpha])
    return _d(lo), _d(hi)


def _drawdown(trades: Sequence[ClosedTrade], capital: Decimal) -> tuple[Decimal, Decimal]:
    """Макс. просадка (% от капитала) и её длительность в днях по кривой закрытых сделок."""
    equity = capital
    peak = capital
    peak_at: datetime | None = None
    max_dd = Decimal(0)
    max_dur = timedelta(0)
    for t in sorted(trades, key=lambda x: x.closed_at):
        equity += t.pnl_net
        if equity >= peak:
            if peak_at is not None:
                max_dur = max(max_dur, t.closed_at - peak_at)
            peak, peak_at = equity, t.closed_at
        else:
            if peak_at is None:
                peak_at = t.opened_at
            max_dd = max(max_dd, (peak - equity) / peak * 100)
            max_dur = max(max_dur, t.closed_at - peak_at)
    return max_dd, _d(max_dur.total_seconds() / 86400)


def _daily_ratios(
    trades: Sequence[ClosedTrade], capital: Decimal, window: tuple[datetime, datetime]
) -> tuple[Decimal | None, Decimal | None]:
    """Sharpe / Sortino по дневным доходностям (безриск 0, годовая база 365)."""
    days = max(1, math.ceil((window[1] - window[0]) / _DAY))
    if days < 2:
        return None, None
    per_day: dict[int, float] = defaultdict(float)
    for t in trades:
        per_day[(t.closed_at - window[0]) // _DAY] += float(t.pnl_net / capital)
    returns = np.array([per_day.get(i, 0.0) for i in range(days)])
    std = returns.std(ddof=1)
    mean = returns.mean()
    sharpe = _d(mean / std * math.sqrt(365)) if std > 0 else None
    downside = returns[returns < 0]
    dstd = math.sqrt((downside**2).sum() / (len(returns) - 1)) if len(downside) else 0.0
    sortino = _d(mean / dstd * math.sqrt(365)) if dstd > 0 else None
    return sharpe, sortino


def _exposure_pct(trades: Sequence[ClosedTrade], window: tuple[datetime, datetime]) -> Decimal:
    total = (window[1] - window[0]).total_seconds()
    if total <= 0 or not trades:
        return Decimal(0)
    intervals = sorted((t.opened_at, t.closed_at) for t in trades)
    covered = 0.0
    cur_from, cur_to = intervals[0]
    for a, b in intervals[1:]:
        if a <= cur_to:
            cur_to = max(cur_to, b)
        else:
            covered += (cur_to - cur_from).total_seconds()
            cur_from, cur_to = a, b
    covered += (cur_to - cur_from).total_seconds()
    return _d(covered / total * 100)


def metrics(
    trades: Sequence[ClosedTrade],
    benchmark: Sequence[Candle] | Decimal | None = None,
    *,
    capital: Decimal,
    window: tuple[datetime, datetime],
    config: ThresholdConfig | None = None,
    seed: int = 0,
    extra: dict[str, object] | None = None,
) -> Metrics:
    """Словарь метрик по закрытым сделкам. `benchmark` — свечи BTC за окно или готовый % B&H.
    `extra` — ветко-специфичные значения (copy_lag_cost, brier, ...), если ветка их даёт."""
    cfg = config or load_threshold()
    n = len(trades)
    pnls = [t.pnl_net for t in trades]
    net = sum(pnls, Decimal(0))
    net_pct = net / capital * 100
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins, Decimal(0))
    gross_loss = -sum(losses, Decimal(0))

    breakdown = CostsBreakdown(
        fee=sum((t.costs.fee for t in trades), Decimal(0)),
        slippage=sum((t.costs.slippage for t in trades), Decimal(0)),
        funding=sum((t.costs.funding for t in trades), Decimal(0)),
        gas=sum((t.costs.gas for t in trades), Decimal(0)),
        priority_fee=sum((t.costs.priority_fee for t in trades), Decimal(0)),
        royalty=sum((t.costs.royalty for t in trades), Decimal(0)),
        total=sum((t.costs.total for t in trades), Decimal(0)),
        turnover=sum((t.turnover for t in trades), Decimal(0)),
    )
    max_dd, dd_days = _drawdown(trades, capital)
    sharpe, sortino = _daily_ratios(trades, capital, window)

    if isinstance(benchmark, Decimal):
        bh: Decimal | None = benchmark
    elif benchmark:
        bh = btc_buy_and_hold_pct(benchmark)
    else:
        bh = None

    cagr = annualized_pct(net_pct, window)
    bench_cagr = annualized_pct(bh, window)

    values: dict[str, object] = dict(_NA)
    outcome: dict[str, object] = {}
    for key, val in (extra or {}).items():
        if key in values:
            values[key] = val
        elif key in _OUTCOME_KEYS:
            # Не ветко-специфичная метрика, а факт о ходе прогона (сработал стоп стратегии).
            # Без этой ветки такие ключи молча терялись: `values` принимает только имена из `_NA`.
            outcome[key] = val

    return Metrics(
        n_trades=n,
        sample=sample_status(n, cfg.min_trades),
        net_pnl=net,
        net_pnl_pct=net_pct,
        ev_per_trade=net / n if n else None,
        ev_ci95=_bootstrap_ci(pnls, cfg.bootstrap_samples, cfg.confidence, seed) if n else None,
        win_rate=Decimal(len(wins)) / n if n else None,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        payoff=(
            (gross_profit / len(wins)) / (gross_loss / len(losses)) if wins and losses else None
        ),
        max_dd_pct=max_dd,
        dd_duration_days=dd_days,
        sharpe=sharpe,
        sortino=sortino,
        exposure_pct=_exposure_pct(trades, window),
        costs_pct=(breakdown.total / breakdown.turnover * 100) if breakdown.turnover else None,
        costs=breakdown,
        benchmark_pct=bh,
        vs_benchmark=(net_pct - bh) if bh is not None else None,
        cagr_pct=cagr,
        benchmark_cagr_pct=bench_cagr,
        vs_benchmark_cagr=(
            (cagr - bench_cagr) if (cagr is not None and bench_cagr is not None) else None
        ),
        capital=capital,
        window_from=window[0],
        window_to=window[1],
        **outcome,  # type: ignore[arg-type]
        **values,  # type: ignore[arg-type]
    )


def _crit(
    name: str, value: Decimal | int | None, limit: Decimal | int | None, op: str, detail: str = ""
) -> Criterion:
    passed: bool | None
    if value is None or limit is None:
        passed = None
    elif op == ">=":
        passed = value >= limit
    elif op == ">":
        passed = value > limit
    elif op == "<=":
        passed = value <= limit
    else:
        raise ValueError(op)
    return Criterion(name=name, value=value, limit=limit, op=op, passed=passed, detail=detail)


def threshold(
    m: Metrics,
    branch: Branch | str,
    *,
    rung: Rung | str | None = None,
    config: ThresholdConfig | None = None,
    limits_max_dd: dict[str, Decimal] | None = None,
) -> ThresholdResult:
    """Правило В12: n_trades ≥ 30, EV > 0 (для auto — и нижняя граница CI95 > 0),
    max_dd ≤ лимит группы ветки, vs_benchmark > 0. При недостатке сделок — `insufficient`."""
    cfg = config or load_threshold()
    group = load_limits().group_of(branch)
    dd_table = limits_max_dd or cfg.max_dd_pct
    dd_limit = dd_table[group]
    sample = sample_status(m.n_trades, cfg.min_trades)
    criteria = [
        _crit("n_trades", m.n_trades, cfg.min_trades, ">=", sample.detail),
        _crit("ev_per_trade", m.ev_per_trade, cfg.min_ev_after_costs, ">", "после издержек"),
    ]
    if rung is not None and Rung(rung) == Rung.AUTO:
        lo = m.ev_ci95[0] if m.ev_ci95 else None
        criteria.append(_crit("ev_ci95_low", lo, cfg.min_ev_after_costs, ">", "бутстрап CI95"))
    criteria.append(_crit("max_dd_pct", m.max_dd_pct, dd_limit, "<=", f"лимит группы {group}"))
    # Сравнение с бенчмарком — в ГОДОВЫХ. Разница процентов за окно зависит от длины окна
    # и на длинной истории бессмысленна: BTC с 2011 года дал +2 855 000%, и её не перебьёт
    # ничто. Годовые сопоставимы между окнами: «+24% в год против +100% в год».
    criteria.append(
        _crit(
            "vs_benchmark",
            _vs_benchmark_value(m),
            Decimal(0),
            ">",
            _vs_benchmark_detail(m),
        )
    )
    # Устойчивость: считается не всегда (дорого), поэтому критерии добавляются только когда
    # оценка есть и окон набралось достаточно. Нет оценки — порог работает как раньше,
    # по одному окну; это видно по отсутствию критериев в списке, а не по молчаливому «ок».
    st = m.stability
    if st is not None and st.windows >= cfg.stability.min_windows:
        criteria.append(
            _crit(
                "stability_profitable",
                st.profitable_pct,
                cfg.stability.min_profitable_pct,
                ">=",
                f"зарабатывает в {st.profitable} окнах из {st.windows}",
            )
        )
        criteria.append(
            _crit(
                "stability_vs_benchmark",
                st.ahead_pct,
                cfg.stability.min_ahead_pct,
                ">=",
                f"впереди бенчмарка в {st.ahead} окнах из {st.compared}"
                if st.compared
                else "бенчмарка не было ни в одном окне",
            )
        )

    status: Literal["passed", "failed", "insufficient"]
    if sample.status == "insufficient":
        status = "insufficient"
    elif any(c.passed is False for c in criteria):
        status = "failed"
    elif any(c.passed is None for c in criteria):
        # критерий не из чего посчитать (нет бенчмарка за окно) — нехватка данных, не провал
        status = "insufficient"
    else:
        status = "passed"
    return ThresholdResult(status=status, criteria=criteria, sample=sample)
