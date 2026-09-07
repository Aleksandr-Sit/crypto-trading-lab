"""Сборка данных для экранов: реестр, последние замеры, порог, кривая капитала.

Один словарь метрик для всех веток (R34i): колонки `METRIC_COLUMNS` — те же в таблице
и в карточке; ветко-специфичные (`BRANCH_METRICS`) — отдельным блоком в карточке.
Метрики берутся из снимка `measurements.metrics_json` (таск 02), не пересчитываются.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lab.contracts import Rung, Status
from lab.core.journal import Journal, TradeRecord
from lab.core.ladder import Ladder, Transition, default_threshold_fn
from lab.core.measure import Measurement, Metrics, NotApplicable, ThresholdResult
from lab.core.measure.runner import _row_to_measurement
from lab.core.registry import Candidate, Registry, Strategy
from lab.db.models import AllocationRow, MeasurementRow, RungTransitionRow, StrategyRow

SAMARA = ZoneInfo("Europe/Samara")
NA = "—"
WEEK = timedelta(days=7)

# (ключ, подпись, формат): формат — money | pct | num | int | ratio
METRIC_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("n_trades", "Сделок", "int"),
    ("net_pnl", "Итог, USD", "money"),
    ("net_pnl_pct", "Итог, %", "pct"),
    ("ev_per_trade", "EV/сделку, USD", "money"),
    ("win_rate", "Доля прибыльных", "ratio"),
    ("profit_factor", "Profit factor", "num"),
    ("payoff", "Payoff", "num"),
    ("max_dd_pct", "Макс. просадка, %", "pct"),
    ("dd_duration_days", "Просадка, дней", "num"),
    ("sharpe", "Sharpe", "num"),
    ("sortino", "Sortino", "num"),
    ("exposure_pct", "В рынке, %", "pct"),
    ("costs_pct", "Издержки, %", "pct"),
    ("vs_btc", "vs BTC, п.п.", "pct"),
)
BRANCH_METRICS: tuple[tuple[str, str, str], ...] = (
    ("paper_vs_live_gap", "Разрыв бумага/реальность, %", "pct"),
    ("copy_lag_cost", "Потеря от задержки (copy)", "lag"),
    ("brier", "Brier (prediction)", "num"),
    ("resolution_return", "Доход на резолюции (prediction)", "pct"),
    ("mint_success_rate", "Успешных минтов (nft)", "ratio"),
    ("mint_cost_failed", "Цена неудачных минтов, USD", "money"),
)
SORTABLE = {"id", "branch", "rung", "status", *(k for k, _, _ in METRIC_COLUMNS)}

BRANCH_LABELS = {
    "cex-spot": "CEX спот",
    "cex-perp": "CEX перп",
    "dex-perp": "DEX перп",
    "copy": "Копитрейдинг",
    "meme": "Мем-коины",
    "nft": "NFT",
    "prediction": "Прогнозы",
    "rh": "Robinhood",
}
RUNG_LABELS = {
    "backtest": "бэктест",
    "paper": "бумага",
    "micro": "микро",
    "signal": "сигнал",
    "semi": "полуавто",
    "auto": "авто",
}
STATUS_LABELS = {
    "candidate": "кандидат",
    "measuring": "измеряется",
    "passed": "прошла",
    "failed": "не прошла",
    "degraded": "деградация",
    "retired": "снята",
}
CRITERION_LABELS = {
    "n_trades": "сделок",
    "ev_per_trade": "EV на сделку",
    "ev_ci95": "нижняя граница CI95 EV",
    "ev_ci95_low": "нижняя граница CI95 EV",
    "max_dd_pct": "макс. просадка, %",
    "vs_btc": "vs BTC, п.п.",
}
OPS = {">=": "≥", "<=": "≤", ">": ">", "<": "<", "==": "="}
# Префиксы причин переходов, которые пишет core.ladder.evaluate
PASSED_PREFIX = "порог пройден"
FAILED_PREFIX = "порог не пройден"


# ---------------------------------------------------------------- форматирование


def fmt(value: Any, kind: str = "num") -> str:
    """Единицы одинаковые для всех веток: USD, %, доли. None/NotApplicable → «—»/«н/д»."""
    if value is None:
        return NA
    if isinstance(value, NotApplicable):
        return "н/д"
    if isinstance(value, dict) and kind == "lag":
        return ", ".join(f"{k} с: {fmt(v, 'pct')}" for k, v in value.items()) or NA
    if isinstance(value, dict) and "reason" in value:
        return "н/д"
    if isinstance(value, dict):
        return NA
    if kind == "int":
        return str(int(value))
    d = Decimal(str(value))
    if kind == "money":
        return f"{d:,.2f}".replace(",", " ")
    if kind == "pct":
        return f"{d:+.2f}" if d != 0 else "0.00"
    if kind == "ratio":
        return f"{d * 100:.0f} %"
    return f"{d:.2f}"


def fmt_ci(ci: Any) -> str:
    if not ci:
        return NA
    lo, hi = ci
    return f"[{fmt(lo, 'money')}; {fmt(hi, 'money')}]"


def samara(ts: datetime | None, with_time: bool = True) -> str:
    if ts is None:
        return NA
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    local = ts.astimezone(SAMARA)
    return local.strftime("%d.%m.%Y %H:%M") if with_time else local.strftime("%d.%m.%Y")


def label(kind: str, value: Any) -> str:
    table = {"branch": BRANCH_LABELS, "rung": RUNG_LABELS, "status": STATUS_LABELS}[kind]
    return table.get(str(value), str(value))


# ---------------------------------------------------------------- замеры


def latest_measurements(session: Session, strategy_ids: list[str]) -> dict[str, Measurement]:
    """Последний замер каждой стратегии (любой режим) из снимка в базе."""
    if not strategy_ids:
        return {}
    last = (
        select(MeasurementRow.strategy_id, func.max(MeasurementRow.id).label("mid"))
        .where(MeasurementRow.strategy_id.in_(strategy_ids))
        .group_by(MeasurementRow.strategy_id)
        .subquery()
    )
    rows = session.scalars(select(MeasurementRow).join(last, MeasurementRow.id == last.c.mid))
    return {r.strategy_id: _row_to_measurement(r, cached=True) for r in rows}


def measurements_of(session: Session, strategy_id: str) -> list[Measurement]:
    rows = session.scalars(
        select(MeasurementRow)
        .where(MeasurementRow.strategy_id == strategy_id)
        .order_by(MeasurementRow.created_at.desc(), MeasurementRow.id.desc())
    )
    return [_row_to_measurement(r, cached=True) for r in rows]


def metric_value(m: Metrics | None, key: str) -> Any:
    return None if m is None else getattr(m, key, None)


def sample_text(m: Measurement | None) -> str | None:
    """«недостаточно данных: N из 30» (R11.1) — только когда выборки не хватает."""
    if m is None or m.metrics is None:
        return None
    s = m.metrics.sample
    if s.status == "insufficient":
        return f"недостаточно данных: {s.n} из {s.required}"
    return None


@dataclass
class StrategyLine:
    strategy: Strategy
    measurement: Measurement | None
    cells: dict[str, str] = field(default_factory=dict)
    sort_keys: dict[str, Any] = field(default_factory=dict)

    @property
    def sample(self) -> str | None:
        return sample_text(self.measurement)

    @property
    def measured_at(self) -> str:
        return samara(self.measurement.created_at) if self.measurement else NA


def strategy_lines(
    session: Session,
    *,
    branch: str | None = None,
    rung: str | None = None,
    status: str | None = None,
    sort: str = "id",
    desc: bool = False,
) -> list[StrategyLine]:
    reg = Registry(session)
    strategies = reg.list(branch=branch or None, rung=rung or None, status=status or None)
    latest = latest_measurements(session, [s.id for s in strategies])
    lines: list[StrategyLine] = []
    for s in strategies:
        m = latest.get(s.id)
        metrics = m.metrics if m else None
        line = StrategyLine(s, m)
        for key, _, kind in METRIC_COLUMNS:
            v = metric_value(metrics, key)
            line.cells[key] = fmt(v, kind)
            line.sort_keys[key] = v if isinstance(v, int | Decimal) else None
        line.sort_keys.update(
            id=s.id, branch=s.branch.value, rung=s.rung.value, status=s.status.value
        )
        lines.append(line)
    key = sort if sort in SORTABLE else "id"
    if key in {"id", "branch", "rung", "status"}:
        lines.sort(key=lambda ln: ln.sort_keys[key], reverse=desc)
        return lines
    # метрики: стратегии без замера — всегда в конце, независимо от направления
    with_value = [ln for ln in lines if ln.sort_keys.get(key) is not None]
    without = [ln for ln in lines if ln.sort_keys.get(key) is None]
    with_value.sort(key=lambda ln: ln.sort_keys[key], reverse=desc)
    return with_value + without


# ---------------------------------------------------------------- карточка


@dataclass
class CriterionLine:
    name: str
    value: str
    limit: str
    op: str
    passed: bool | None
    detail: str

    @property
    def mark(self) -> str:
        return "✓" if self.passed else ("✗" if self.passed is False else "?")


def criteria_lines(t: ThresholdResult | None) -> list[CriterionLine]:
    if t is None:
        return []
    out = []
    for c in t.criteria:
        if c.name == "n_trades":
            kind = "int"
        elif "pct" in c.name or c.name == "vs_btc":
            kind = "pct"
        else:
            kind = "money"
        out.append(
            CriterionLine(
                name=CRITERION_LABELS.get(c.name, c.name),
                value=fmt(c.value, kind),
                limit=fmt(c.limit, kind),
                op=OPS.get(c.op, c.op),
                passed=c.passed,
                detail=c.detail,
            )
        )
    return out


def equity_points(trades: list[TradeRecord] | list[Any]) -> list[tuple[datetime, Decimal]]:
    """Кривая капитала = накопленный net P&L по закрытым сделкам в порядке закрытия."""
    closed = [t for t in trades if getattr(t, "closed_at", None) is not None]
    closed.sort(key=lambda t: t.closed_at)
    total = Decimal(0)
    points: list[tuple[datetime, Decimal]] = []
    for t in closed:
        total += Decimal(t.pnl_net)
        points.append((t.closed_at, total))
    return points


@dataclass
class Card:
    strategy: Strategy
    latest: Measurement | None
    measurements: list[Measurement]
    trades: list[TradeRecord]
    open_trades: list[TradeRecord]
    transitions: list[Transition]
    curve: list[tuple[datetime, Decimal]]

    @property
    def metrics(self) -> Metrics | None:
        return self.latest.metrics if self.latest else None

    @property
    def sample(self) -> str | None:
        return sample_text(self.latest)

    @property
    def criteria(self) -> list[CriterionLine]:
        return criteria_lines(self.latest.threshold if self.latest else None)

    @property
    def threshold_status(self) -> str | None:
        return self.latest.threshold.status if self.latest and self.latest.threshold else None

    @property
    def common_rows(self) -> list[tuple[str, str]]:
        return [(lbl, fmt(metric_value(self.metrics, k), kind)) for k, lbl, kind in METRIC_COLUMNS]

    @property
    def branch_rows(self) -> list[tuple[str, str]]:
        return [(lbl, fmt(metric_value(self.metrics, k), kind)) for k, lbl, kind in BRANCH_METRICS]

    @property
    def ev_ci95(self) -> str:
        return fmt_ci(self.metrics.ev_ci95) if self.metrics else NA

    @property
    def forward_only(self) -> bool:
        return not self.strategy.can_backtest

    @property
    def forward_counter(self) -> str:
        """Сколько форварда накоплено: сделок и дней (R12.2)."""
        n = len(self.trades)
        stamps = [t.opened_at for t in self.trades + self.open_trades]
        if not stamps:
            return "0 сделок, 0 дней"
        first = min(stamps)
        if first.tzinfo is None:
            first = first.replace(tzinfo=UTC)
        days = max(0, (datetime.now(UTC) - first).days)
        return f"{n} сделок, {days} дней"


def card(session: Session, strategy_id: str) -> Card:
    reg = Registry(session)
    strategy = reg.get(strategy_id)
    journal = Journal(session)
    measurements = measurements_of(session, strategy_id)
    latest = measurements[0] if measurements else None
    trades = journal.closed_trades(strategy_id)
    curve = equity_points(trades)
    if not curve and latest and latest.trades:
        curve = equity_points(latest.trades)
    ladder = Ladder(session, threshold=default_threshold_fn())
    return Card(
        strategy=strategy,
        latest=latest,
        measurements=measurements,
        trades=trades,
        open_trades=journal.open_trades(strategy_id),
        transitions=ladder.history(strategy_id),
        curve=curve,
    )


# ---------------------------------------------------------------- сводка


@dataclass
class Summary:
    allocations: list[AllocationRow]
    passed_week: list[Strategy]
    failed_week: list[Strategy]
    transitions_week: list[RungTransitionRow]
    pending_candidates: list[Candidate]
    awaiting_operator: list[Strategy]
    degraded: list[Strategy]
    total_strategies: int

    @property
    def registry_empty(self) -> bool:
        return self.total_strategies == 0

    @property
    def decisions_count(self) -> int:
        return len(self.pending_candidates) + len(self.awaiting_operator) + len(self.degraded)


def summary(session: Session, now: datetime | None = None) -> Summary:
    now = now or datetime.now(UTC)
    since = now - WEEK
    reg = Registry(session)
    all_strategies = reg.list()
    by_id = {s.id: s for s in all_strategies}
    recent = {
        r.id
        for r in session.scalars(select(StrategyRow).where(StrategyRow.updated_at >= since))
    }
    transitions = list(
        session.scalars(
            select(RungTransitionRow)
            .where(RungTransitionRow.ts >= since)
            .order_by(RungTransitionRow.ts.desc())
        )
    )
    # Пройденный порог поднимает ступень и возвращает статус `measuring` — поэтому
    # «прошло/упало» читается из переходов лестницы, а не только из текущего статуса.
    passed_ids = {t.strategy_id for t in transitions if t.reason.startswith(PASSED_PREFIX)}
    failed_ids = {t.strategy_id for t in transitions if t.reason.startswith(FAILED_PREFIX)}
    passed_ids |= {s.id for s in all_strategies if s.status == Status.PASSED and s.id in recent}
    failed_ids |= {s.id for s in all_strategies if s.status == Status.FAILED and s.id in recent}
    passed = [by_id[i] for i in sorted(passed_ids) if i in by_id]
    failed = [by_id[i] for i in sorted(failed_ids - passed_ids) if i in by_id]
    allocations = list(session.scalars(select(AllocationRow).order_by(AllocationRow.branch)))
    return Summary(
        allocations=allocations,
        passed_week=passed,
        failed_week=failed,
        transitions_week=transitions,
        pending_candidates=reg.candidates("pending"),
        awaiting_operator=[
            s for s in all_strategies if s.rung == Rung.SEMI and s.status == Status.PASSED
        ],
        degraded=[s for s in all_strategies if s.status == Status.DEGRADED],
        total_strategies=len(all_strategies),
    )


# ---------------------------------------------------------------- кладбище


@dataclass
class GraveLine:
    strategy: Strategy
    reason: str
    when: str


def grave_reason(session: Session, s: Strategy, latest: Measurement | None) -> str:
    if s.retired_reason:
        return s.retired_reason
    last = session.scalars(
        select(RungTransitionRow)
        .where(RungTransitionRow.strategy_id == s.id)
        .order_by(RungTransitionRow.ts.desc(), RungTransitionRow.id.desc())
        .limit(1)
    ).first()
    if last is not None and last.reason:
        return last.reason
    if latest and latest.threshold and latest.threshold.failed_names():
        names = ", ".join(CRITERION_LABELS.get(n, n) for n in latest.threshold.failed_names())
        return f"порог не пройден: {names}"
    if latest and latest.status == "incomplete" and latest.reason:
        return latest.reason
    return "причина не записана"


def graveyard(session: Session, *, reason: str = "") -> tuple[list[GraveLine], list[str]]:
    """`failed`/`retired` с причиной (A02); фильтр — подстрока причины."""
    reg = Registry(session)
    dead = reg.list(status=Status.FAILED) + reg.list(status=Status.RETIRED)
    latest = latest_measurements(session, [s.id for s in dead])
    lines = [
        GraveLine(s, grave_reason(session, s, latest.get(s.id)), samara(s.created_at, False))
        for s in dead
    ]
    reasons = sorted({ln.reason for ln in lines})
    needle = reason.strip().lower()
    if needle:
        lines = [ln for ln in lines if needle in ln.reason.lower()]
    return lines, reasons


__all__ = [
    "BRANCH_METRICS",
    "METRIC_COLUMNS",
    "Card",
    "GraveLine",
    "StrategyLine",
    "Summary",
    "card",
    "equity_points",
    "fmt",
    "graveyard",
    "label",
    "samara",
    "strategy_lines",
    "summary",
]
