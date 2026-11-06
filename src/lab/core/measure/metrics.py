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
from itertools import groupby
from typing import Literal

import numpy as np

from lab.config import ThresholdConfig, load_limits, load_threshold
from lab.contracts import Branch, Candle, Rung
from lab.core.measure.stress import METHOD as STRESS_METHOD
from lab.core.measure.stress import cost_stress
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
    "execution",
    "trade_source",
    "forward_verified",
    "observation_id",
    "rules_revision",
    "observation_started_at",
    "forward_excluded",
    "virtual_state",
    "stopped_at",
    "stop_rule",
    "blocked_signals",
    "benchmark_kind",
    "benchmark_max_dd_pct",
    "allocation",
    "stability",
    "stability_error",
    "research",
    "reasons",
    "data_gaps",
    "liquidations",
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


def benchmark_drawdown_pct(candles: Sequence[Candle]) -> Decimal | None:
    """Просадка самого бенчмарка за окно, в процентах (положительное число).

    Нужна правилам размещения: их планка относительная. Считается по закрытиям —
    внутридневной минимум сюда не входит намеренно, потому что и просадка стратегии
    считается по переоценке на баре, и сравнивать надо одинаково измеренные величины.
    """
    if not candles:
        return None
    peak = candles[0].close
    worst = Decimal(0)
    for candle in candles:
        if candle.close > peak:
            peak = candle.close
        if peak > 0:
            worst = min(worst, candle.close / peak - 1)
    return -worst * 100


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


def _own_cagr(m: Metrics) -> Decimal | None:
    """Доходность стратегии для сравнения: с поправкой на простаивающий капитал, если она есть.

    Без поправки сравнение несимметрично: бенчмарк «кэш» приносит безрисковую ставку
    на весь счёт, а незанятые деньги стратегии — ноль.
    """
    return m.cagr_with_idle_pct if m.cagr_with_idle_pct is not None else m.cagr_pct


def vs_benchmark_value(m: Metrics) -> Decimal | None:
    """Насколько стратегия лучше бенчмарка.

    В годовых, если их можно посчитать, иначе — в процентах за окно (короткое окно,
    капитал в нуле). Знак от единицы не зависит: годовые — монотонное преобразование
    доходности за то же окно, поэтому ВЕРДИКТ порога не меняется, меняется читаемость.
    Именно поэтому смена единицы — не решение проблемы «бенчмарк с 2011 недостижим»;
    решает её сравнение по скользящим окнам (`lab measure rolling`).
    """
    own = _own_cagr(m)
    if own is not None and m.benchmark_cagr_pct is not None:
        return own - m.benchmark_cagr_pct
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


def _bootstrap_means(
    trades: Sequence[ClosedTrade], samples: int, seed: int, block_groups: int | None,
) -> tuple[np.ndarray, int, int]:
    """Circular blocks of consecutive closing timestamps; simultaneous legs stay together.

    Resample sums AND counts: the statistic remains closed net P&L per trade,
    not the equally weighted mean of timestamp means. Memory is O(samples + groups).
    """
    sums, counts = [], []
    for _, rows in groupby(sorted(trades, key=_closed_at), key=_closed_at):
        rows = list(rows)
        sums.append(float(sum((t.pnl_net for t in rows), Decimal(0))))
        counts.append(len(rows))
    groups = len(sums)
    if groups < 2:
        return np.array([]), groups, 0
    block = min(block_groups or max(2, math.ceil(groups ** (1 / 3))), groups - 1)
    # A single timestamp cannot establish temporal uncertainty.
    if block < 2:
        return np.array([]), groups, block
    sums_arr, counts_arr = np.asarray(sums), np.asarray(counts)
    # Prefix sums over a doubled circular series avoid samples × trades allocations.
    ps = np.concatenate(([0.0], np.cumsum(np.tile(sums_arr, 2))))
    pc = np.concatenate(([0], np.cumsum(np.tile(counts_arr, 2))))
    rng = np.random.default_rng(seed)
    total, number = np.zeros(samples), np.zeros(samples)
    for offset in range(0, groups, block):
        width = min(block, groups - offset)
        starts = rng.integers(0, groups, size=samples)
        total += ps[starts + width] - ps[starts]
        number += pc[starts + width] - pc[starts]
    return total / number, groups, block


def _interval(means: np.ndarray, confidence: Decimal) -> tuple[Decimal, Decimal] | None:
    if not len(means):
        return None
    alpha = (1 - float(confidence)) / 2
    lo, hi = np.quantile(means, [alpha, 1 - alpha])
    return _d(lo), _d(hi)


def refresh_search_interval(m: Metrics, trades: Sequence[ClosedTrade], variants: int) -> Metrics:
    """Recompute after stability added trials, without altering the nominal interval."""
    if not m.research.get("tracked") or not m.bootstrap:
        return m
    info = dict(m.bootstrap)
    confidence = 1 - (1 - Decimal(info["confidence"])) / max(1, variants)
    means, _, _ = _bootstrap_means(
        trades, info["samples"], info["seed"], info["block_groups"] or None,
    )
    enough_blocks = info["block_groups"] > 0 and (
        info["groups"] // info["block_groups"] >= info["minimum_blocks"]
    )
    ci = (
        _interval(means, confidence)
        if enough_blocks and info["samples"] * (1 - confidence) / 2 >= 5 else None
    )
    info.update(search_confidence=str(confidence), variants=variants)
    return m.model_copy(update={
        "research": {**m.research, "variants": variants}, "bootstrap": info, "ev_search_ci": ci,
    })


def _closed_at(trade: ClosedTrade) -> datetime:
    return trade.closed_at


def _drawdown(trades: Sequence[ClosedTrade], capital: Decimal) -> tuple[Decimal, Decimal]:
    """Макс. просадка (% от капитала) и её длительность в днях по кривой закрытых сделок.

    Сделки, закрытые ОДНИМ моментом, дают один шаг кривой. Иначе у хеджированной связки
    убыточная нога успевает создать провал до того, как в кривую попадёт прибыльная:
    у кэш-энд-керри так набегала «просадка» 20% при итоге связки в пару сотен долларов
    — и на ней же срабатывал стоп, обрывая замер на середине.
    """
    equity = capital
    peak = capital
    peak_at: datetime | None = None
    max_dd = Decimal(0)
    max_dur = timedelta(0)
    for closed_at, batch in groupby(sorted(trades, key=lambda x: x.closed_at), key=_closed_at):
        rows = list(batch)
        equity += sum((t.pnl_net for t in rows), Decimal(0))
        if equity >= peak:
            if peak_at is not None:
                max_dur = max(max_dur, closed_at - peak_at)
            peak, peak_at = equity, closed_at
        else:
            if peak_at is None:
                peak_at = min(t.opened_at for t in rows)
            max_dd = max(max_dd, (peak - equity) / peak * 100)
            max_dur = max(max_dur, closed_at - peak_at)
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
    means, groups, block = _bootstrap_means(
        trades, cfg.bootstrap_samples, seed, cfg.bootstrap_block_groups,
    )
    research = dict((extra or {}).get("research") or {})
    trials = max(1, int(research.get("variants", 1)))
    adjusted_confidence = 1 - (1 - cfg.confidence) / trials
    # Extreme quantiles cannot be resolved by a handful of bootstrap draws.
    search_ci = (
        _interval(means, adjusted_confidence)
        if research.get("tracked") and block > 0 and groups // block >= cfg.bootstrap_min_blocks
        and cfg.bootstrap_samples * (1 - adjusted_confidence) / 2 >= 5
        else None
    )
    # Переоценка по рынку (ревизия 12.09.2026): незакрытая на конец окна позиция входит
    # в итог, а просадка берётся худшая из двух — по закрытым сделкам и по цене бара.
    # До этого позиция, просевшая на 40% и закрытая в +1%, просадки не давала вовсе.
    ex = dict(extra or {})
    unrealized = _d(ex.pop("unrealized_pnl", 0) or 0)
    mtm_raw = ex.pop("mtm_max_dd_pct", None)
    mtm_dd = _d(mtm_raw) if mtm_raw is not None else None
    avg_margin = _d(ex.pop("avg_margin", 0) or 0)
    net = sum(pnls, Decimal(0)) + unrealized
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
    closed_dd, dd_days = _drawdown(trades, capital)
    max_dd = max(closed_dd, mtm_dd) if mtm_dd is not None else closed_dd
    sharpe, sortino = _daily_ratios(trades, capital, window)

    bench_dd: Decimal | None = None
    if isinstance(benchmark, Decimal):
        bh: Decimal | None = benchmark
    elif benchmark:
        bh = btc_buy_and_hold_pct(benchmark)
        bench_dd = benchmark_drawdown_pct(benchmark)
    else:
        bh = None

    cagr = annualized_pct(net_pct, window)
    bench_cagr = annualized_pct(bh, window)
    # Поправка на ПРОСТАИВАЮЩИЙ капитал. Бенчмарк «кэш» считается по безрисковой ставке,
    # а не по нулю — это уже исправлено; но деньги самой стратегии, не занятые залогом,
    # до сих пор приносили ноль. Стратегия, держащая залогом десятую часть счёта,
    # сравнивалась с депозитом на весь счёт, и это сравнение разного с разным.
    # Поправка поднимает ВСЕ стратегии с низкой занятостью капитала, включая отвергнутые.
    idle_pct: Decimal | None = None
    cagr_idle: Decimal | None = None
    if avg_margin > 0 and capital > 0:
        idle_pct = max(Decimal(0), (capital - avg_margin) / capital * 100)
        rate = load_threshold().benchmark.risk_free_annual_pct
        if cagr is not None and rate:
            cagr_idle = cagr + _d(rate) * idle_pct / 100

    values: dict[str, object] = dict(_NA)
    # Просадка бенчмарка: по краевым свечам её не посчитать (для `btc_bh` в замер идут
    # ровно две), а для `btc_dca` бенчмарк вовсе приходит числом. Поэтому настоящее
    # значение приходит СНАРУЖИ, из `ops.measure`, где есть хранилище; здесь остаётся
    # расчёт по ряду — им пользуются прямые вызовы и тесты.
    outcome: dict[str, object] = {} if bench_dd is None else {"benchmark_max_dd_pct": bench_dd}
    for key, val in ex.items():
        if key in values:
            values[key] = val
        elif key in _OUTCOME_KEYS:
            # Не ветко-специфичная метрика, а факт о ходе прогона (сработал стоп стратегии).
            # Без этой ветки такие ключи молча терялись: `values` принимает только имена из `_NA`.
            outcome[key] = val

    return Metrics(
        n_trades=n,
        cost_stress=cost_stress(trades, cfg.cost_stress),
        sample=sample_status(n, cfg.min_trades),
        net_pnl=net,
        net_pnl_pct=net_pct,
        ev_per_trade=sum(pnls, Decimal(0)) / n if n else None,
        ev_ci95=_interval(means, cfg.confidence),
        ev_search_ci=search_ci,
        bootstrap={
            "method": "circular_closing_blocks/v1", "statistic": "closed_net_pnl_per_trade",
            "groups": groups, "block_groups": block, "samples": cfg.bootstrap_samples,
            "minimum_blocks": cfg.bootstrap_min_blocks,
            "confidence": str(cfg.confidence), "seed": seed,
            "search_confidence": str(adjusted_confidence),
            "search_correction": "bonferroni", "variants": trials,
        },
        win_rate=Decimal(len(wins)) / n if n else None,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        payoff=(
            (gross_profit / len(wins)) / (gross_loss / len(losses)) if wins and losses else None
        ),
        max_dd_pct=max_dd,
        closed_max_dd_pct=closed_dd,
        mtm_max_dd_pct=mtm_dd,
        unrealized_pnl=unrealized,
        dd_duration_days=dd_days,
        sharpe=sharpe,
        sortino=sortino,
        exposure_pct=_exposure_pct(trades, window),
        costs_pct=(breakdown.total / breakdown.turnover * 100) if breakdown.turnover else None,
        costs=breakdown,
        benchmark_pct=bh,
        vs_benchmark=(net_pct - bh) if bh is not None else None,
        cagr_pct=cagr,
        idle_capital_pct=idle_pct,
        cagr_with_idle_pct=cagr_idle,
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
    if rung is not None and Rung(rung) == Rung.PAPER:
        criteria.append(
            _crit(
                "paper_journal",
                1 if m.trade_source == "journal_paper" else None,
                1,
                ">=",
                "нужны бумажные сделки из журнала; история не подтверждает ступень paper",
            )
        )
    if rung is not None and Rung(rung) in (Rung.SEMI, Rung.AUTO):
        stress = m.cost_stress
        current = (
            stress is not None and stress.method == STRESS_METHOD
            and stress.multiplier == cfg.cost_stress.multiplier
            and stress.extra_turnover_bps == cfg.cost_stress.extra_turnover_bps
            and stress.n_trades == m.n_trades
        )
        criteria.append(_crit(
            "cost_stress_ev", stress.ev_per_trade if current else None,
            cfg.min_ev_after_costs, ">",
            "закрытый EV при увеличенных расходах, уменьшенных поступлениях и "
            "добавке к обороту; нужны результаты актуального стресс-сценария",
        ))
        lo = m.ev_search_ci[0] if m.ev_search_ci else None
        criteria.append(_crit(
            "ev_ci95_low", lo, cfg.min_ev_after_costs, ">",
            "блоковый интервал закрытого EV с поправкой за все зарегистрированные варианты; "
            "нужны журнал поиска и достаточное число бутстрап-выборок",
        ))
    if m.trade_source in ("journal_paper", "journal_live") and rung != Rung.BACKTEST:
        criteria.append(_crit(
            "forward_verified", 1 if m.forward_verified else None, 1, ">=",
            "нужны сделки после начала наблюдения с проверенным временем регистрации; "
            f"исключено: {m.forward_excluded}",
        ))
    # Правила РАЗМЕЩЕНИЯ судятся по относительной планке: они держат 100% в активе и
    # наследуют его просадку, а фиксированные 5% группы `cex` отбраковывали такой класс
    # целиком — и ротацию золото/BTC (просадка 25.2% против 76.6% у самого биткойна),
    # и накопительную лестницу. Решение владельца 14.09.2026: не больше доли от просадки
    # бенчмарка за то же окно.
    #
    # Послабление НЕ бесплатное и требует обоих условий: карточка объявила правило
    # размещением И у бенчмарка есть измеренная просадка. Бенчмарк «кэш» просадки не имеет,
    # поэтому нейтральные стратегии (кэш-энд-керри с его 9.31%) послабления не получают
    # даже с флагом — иначе флаг стал бы способом обойти порог.
    bench_dd = m.benchmark_max_dd_pct
    if m.allocation and bench_dd is not None and bench_dd > 0:
        share = cfg.allocation.max_dd_share_of_benchmark
        dd_limit = bench_dd * share
        dd_detail = (
            f"размещение: не больше {share:.0%} просадки бенчмарка "
            f"({bench_dd:.1f}% за окно)"
        )
    else:
        dd_detail = f"лимит группы {group}"
    criteria.append(_crit("max_dd_pct", m.max_dd_pct, dd_limit, "<=", dd_detail))
    # Сравнение с бенчмарком — в ГОДОВЫХ. Разница процентов за окно зависит от длины окна
    # и на длинной истории бессмысленна: BTC с 2011 года дал +2 855 000%, и её не перебьёт
    # ничто. Годовые сопоставимы между окнами: «+24% в год против +100% в год».
    criteria.append(
        _crit(
            "vs_benchmark",
            vs_benchmark_value(m),
            Decimal(0),
            ">",
            _vs_benchmark_detail(m),
        )
    )
    # Отсутствие расчёта не является доказательством устойчивости.
    st = m.stability
    criteria.append(_crit(
        "stability_windows", st.windows if st is not None else None,
        cfg.stability.min_windows, ">=",
        m.stability_error or "нужен расчёт устойчивости с достаточным числом зачтённых окон",
    ).model_copy(update={
        "passed": True if st is not None and st.windows >= cfg.stability.min_windows
        and not m.stability_error else None,
    }))
    criteria.append(_crit(
        "stability_compared", st.compared if st is not None else None,
        cfg.stability.min_windows, ">=",
        "нужно достаточно окон с измеренным бенчмарком",
    ).model_copy(update={
        "passed": True if st is not None and st.compared >= cfg.stability.min_windows else None,
    }))
    if st is not None and st.windows >= cfg.stability.min_windows:
        criteria.append(
            _crit(
                "stability_profitable",
                st.profitable_pct,
                cfg.stability.min_profitable_pct,
                ">=",
                f"зарабатывает в {st.profitable} окнах из {st.windows} "
                f"(окна перекрываются, независимых ≈ {st.independent})",
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
