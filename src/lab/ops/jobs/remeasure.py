"""Еженедельное переизмерение и срок годности (R14, R14.1).

Раз в неделю все живые стратегии (`measuring`/`passed`) меряются на свежем окне и идут
в `ladder.evaluate`; успешный замер отодвигает `valid_until`. Задание `expiry` смотрит
на просроченные: без успешного переизмерения N недель — `degraded`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from lab.contracts import Rung, Status
from lab.core.ladder.rules import MEASURE_MODE_FOR_RUNG
from lab.db.base import utcnow
from lab.db.models import StrategyRow
from lab.discovery.config import DiscoveryConfig, load_discovery
from lab.discovery.decisions import expire_candidates

log = logging.getLogger(__name__)

LIVE = (Status.MEASURING.value, Status.PASSED.value)
EXPIRY_REASON = "срок годности истёк: нет успешного переизмерения"


@dataclass
class RemeasureReport:
    window: tuple[datetime, datetime]
    measured: list[str] = field(default_factory=list)
    transitions: list[Any] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    # Кого не успели за отведённое время. Пустой список — успели всех.
    skipped: list[str] = field(default_factory=list)

    def text(self) -> str:
        # Дата одна — конец окна: начало у каждой стратегии своё, оно зависит от
        # таймфрейма (дневным правилам нужны годы, минутным хватает квартала).
        head = (
            f"Переизмерение на {self.window[1]:%d.%m.%Y}: "
            f"измерено {len(self.measured)}, переходов {len(self.transitions)}"
        )
        lines = [head]
        if self.skipped:
            lines.append(
                f"⏳ не успели за отведённое время ({len(self.skipped)}): "
                + ", ".join(self.skipped[:5])
                + ("…" if len(self.skipped) > 5 else "")
            )
        for t in self.transitions:
            lines.append(f"• {t.strategy_id}: {t.from_rung} → {t.to_rung} — {t.reason}")
        for sid, err in sorted(self.failed.items()):
            lines.append(f"⚠️ {sid}: {err}")
        return "\n".join(lines)


def weekly_remeasure(
    session_scope: Callable[[], Any],
    *,
    measure: Callable[..., Any],
    ladder_factory: Callable[[Any], Any],
    bot: Any = None,
    config: DiscoveryConfig | None = None,
    now: datetime | None = None,
) -> RemeasureReport:
    """Все живые стратегии → замер на свежем окне → `ladder.evaluate`; отчёт в телегу."""
    cfg = config or load_discovery()
    at = now or utcnow()
    by_tf = dict(getattr(cfg.remeasure, "window_days_by_tf", {}) or {})
    window = (at - timedelta(days=cfg.remeasure.window_days), at)
    report = RemeasureReport(window=window)
    with session_scope() as session:
        ladder = ladder_factory(session)
        rows = list(session.scalars(select(StrategyRow).where(StrategyRow.status.in_(LIVE))))
        # Бюджет времени на весь прогон. Сервер общий: на тех же ядрах живьём торгует
        # соседний проект, а замер одной вселенной из 649 инструментов идёт минутами.
        # Без предела воскресная ночь превращается в многочасовую нагрузку, и виноват
        # окажется не тот, кто её создал. Не успели — честно перечисляем в отчёте.
        budget = timedelta(minutes=int(getattr(cfg.remeasure, "budget_minutes", 60) or 0))
        started = utcnow()
        for row in rows:
            if budget and utcnow() - started > budget:
                report.skipped.append(row.id)
                continue
            # Окно СВОЁ у каждого таймфрейма: 30 сделок порога дневная стратегия за квартал
            # не наберёт физически, и вердикт будет вечный `insufficient` — не потому что
            # стратегия плоха, а потому что её не успели измерить.
            days = by_tf.get(row.timeframe or "", cfg.remeasure.window_days)
            window = (at - timedelta(days=days), at)
            try:
                measured = measure(
                    strategy_id=row.id,
                    mode=MEASURE_MODE_FOR_RUNG[Rung(row.rung)],
                    window=window,
                )
            except Exception as err:  # noqa: BLE001 — одна стратегия не роняет прогон
                report.failed[row.id] = f"{type(err).__name__}: {err}"
                log.warning("Переизмерение %s не удалось: %s", row.id, err)
                continue
            report.measured.append(row.id)
            metrics = getattr(measured, "metrics", None)
            if metrics is not None and getattr(measured, "status", "") == "ok":
                metrics, problem = attach_stability(session_scope, row, metrics, at)
                if problem:
                    report.failed[row.id] = problem
                metrics = save_stability(session, measured, metrics, row)
            else:
                report.failed[row.id] = getattr(measured, "reason", "") or "замер не завершён"
                # evaluate(None) подхватывает старый снимок: новый сбой не должен
                # разрешать повышение по прежнему успешному расчёту устойчивости.
                continue
            row.valid_until = at + timedelta(weeks=cfg.remeasure.valid_weeks)
            transition = ladder.evaluate(row.id, metrics=metrics)
            if transition is not None:
                report.transitions.append(transition)
        session.flush()
    _notify(bot, "Переизмерение", report.text())
    return report


def expiry(
    session_scope: Callable[[], Any],
    *,
    ladder_factory: Callable[[Any], Any],
    bot: Any = None,
    config: DiscoveryConfig | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Просроченные стратегии → `degraded` (R14.1), нерешённые кандидаты → `rejected`.

    Кандидаты живут в том же задании намеренно: это одна и та же мысль о сроке годности,
    и ходить по базе дважды ради неё незачем. Возврат прежний — только стратегии: по ним
    двигается лестница, а число просроченных кандидатов уходит в отчёт.
    """
    cfg = config or load_discovery()
    at = now or utcnow()
    expired: list[str] = []
    stale: list[int] = []
    with session_scope() as session:
        ladder = ladder_factory(session)
        rows = session.scalars(
            select(StrategyRow).where(
                StrategyRow.status.in_(LIVE),
                StrategyRow.valid_until.is_not(None),
                StrategyRow.valid_until < at,
            )
        )
        for row in rows:
            reason = f"{EXPIRY_REASON} {cfg.remeasure.valid_weeks} нед."
            ladder.demote(row.id, reason)
            # `ladder` не выставляет degraded без пробоя стопа, а R14.1 требует именно его.
            row.status = Status.DEGRADED.value
            expired.append(row.id)
        stale = expire_candidates(session, days=cfg.candidate_expire_days, now=at)
        session.flush()
    lines = []
    if expired:
        lines.append("Просрочены и переведены в degraded:\n" + "\n".join(expired))
    if stale:
        days = cfg.candidate_expire_days
        lines.append(f"Кандидатов без решения {days} сут. отклонено: {len(stale)}")
    if lines:
        _notify(bot, "Срок годности", "\n".join(lines))
    return expired


def attach_stability(
    session_scope: Callable[[], Any],
    row: Any,
    metrics: Any,
    at: datetime,
    *,
    stability_fn: Callable[..., Any] | None = None,
) -> tuple[Any, str | None]:
    """Метрики плюс устойчивость, если стратегия прошла остальные критерии порога.

    Второе значение — причина блокировки: ошибка расчёта или недостаток окон.
    """
    from lab.config import load_threshold
    from lab.core.measure import threshold as measure_threshold

    base = measure_threshold(metrics, row.branch, rung=row.rung)
    # Только предварительный отбор для дорогого расчёта. Сам допуск всегда
    # проверяет полный порог, включая обязательную устойчивость.
    if any(c.passed is not True for c in base.criteria if not c.name.startswith("stability_")):
        return metrics, None

    if stability_fn is None:
        from lab.ops.measure import run_stability as stability_fn

    cfg = load_threshold().stability
    try:
        stability, _ = stability_fn(
            row.id,
            session_scope=session_scope,
            now=at,
            window_days=cfg.window_days,
            step_days=cfg.step_days,
        )
    except Exception as err:  # noqa: BLE001 — оценка не обязана быть, решение всё равно нужно
        log.warning("Устойчивость %s не посчиталась: %s", row.id, err)
        problem = f"устойчивость не посчиталась: {type(err).__name__}: {err}"
        return metrics.model_copy(update={"stability": None, "stability_error": problem}), problem
    log.info(
        "Устойчивость %s: в плюс %s из %s окон, впереди бенчмарка %s из %s",
        row.id,
        stability.profitable,
        stability.windows,
        stability.ahead,
        stability.compared,
    )
    problem = None
    if stability.windows < cfg.min_windows:
        problem = (
            f"окон {stability.windows} при минимуме {cfg.min_windows}: "
            "повышение запрещено"
        )
    elif stability.compared < cfg.min_windows:
        problem = f"окон с бенчмарком {stability.compared} при минимуме {cfg.min_windows}"
    return metrics.model_copy(update={"stability": stability, "stability_error": problem}), problem


def save_stability(session: Any, measured: Any, metrics: Any, row: Any) -> Any:
    """Новый неизменяемый снимок: результат расчёта виден show и ручному promote."""
    from lab.core.measure import threshold
    from lab.core.measure.metrics import refresh_search_interval
    from lab.core.measure.research import variant_count
    from lab.core.measure.runner import _persist
    from lab.core.measure.types import Measurement

    if not isinstance(measured, Measurement) or measured.id is None:
        return metrics
    metrics = refresh_search_interval(metrics, measured.trades, variant_count(session))
    _persist(session, measured.model_copy(update={
        "id": None, "cached": False, "metrics": metrics,
        "threshold": threshold(metrics, row.branch, rung=row.rung),
    }))
    return metrics


def _notify(bot: Any, title: str, detail: str) -> None:
    if bot is None:
        return
    bot.send_card_sync("alert", {"title": title, "detail": detail, "service": "worker"})


__all__ = ["EXPIRY_REASON", "RemeasureReport", "attach_stability", "expiry", "weekly_remeasure"]
