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

from lab.contracts import Status
from lab.db.base import utcnow
from lab.db.models import StrategyRow
from lab.discovery.config import DiscoveryConfig, load_discovery

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
        head = (
            f"Переизмерение {self.window[0]:%d.%m}–{self.window[1]:%d.%m}: "
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
            try:
                measured = measure(strategy_id=row.id, mode=cfg.remeasure.mode, window=window)
            except Exception as err:  # noqa: BLE001 — одна стратегия не роняет прогон
                report.failed[row.id] = f"{type(err).__name__}: {err}"
                log.warning("Переизмерение %s не удалось: %s", row.id, err)
                continue
            report.measured.append(row.id)
            row.valid_until = at + timedelta(weeks=cfg.remeasure.valid_weeks)
            metrics = _with_stability(
                session_scope, row, measured, at, session=session, measure=measure
            )
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
    """Просроченные стратегии → `degraded` (R14.1)."""
    cfg = config or load_discovery()
    at = now or utcnow()
    expired: list[str] = []
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
        session.flush()
    if expired:
        _notify(bot, "Срок годности", "Просрочены и переведены в degraded:\n" + "\n".join(expired))
    return expired


def _with_stability(
    session_scope: Callable[[], Any],
    row: StrategyRow,
    measured: Any,
    at: datetime,
    *,
    session: Any,
    measure: Callable[..., Any],
) -> Any:
    """Метрики свежего замера плюс оценка устойчивости — если её вообще есть смысл считать.

    Прогон по скользящим окнам стоит десятков замеров, поэтому он делается ТОЛЬКО для
    стратегий, прошедших остальные критерии: провалившей EV или просадку устойчивость
    ничего не изменит. Сорвался прогон — двигаемся по одиночному замеру, как раньше,
    а не роняем всё переизмерение.
    """
    from lab.config import load_threshold
    from lab.core.measure import threshold as measure_threshold
    from lab.ops.measure import run_stability

    metrics = getattr(measured, "metrics", None)
    if metrics is None or getattr(measured, "status", "") != "ok":
        return None
    base = measure_threshold(metrics, row.branch, rung=row.rung)
    if base.status != "passed":
        return metrics

    cfg = load_threshold().stability
    try:
        stability, _ = run_stability(
            row.id,
            session_scope=session_scope,
            now=at,
            window_days=cfg.window_days,
            step_days=cfg.step_days,
        )
    except Exception as err:  # noqa: BLE001 — оценка не обязана быть, решение всё равно нужно
        log.warning("Устойчивость %s не посчиталась: %s", row.id, err)
        return metrics
    log.info(
        "Устойчивость %s: в плюс %s из %s окон, впереди бенчмарка %s из %s",
        row.id,
        stability.profitable,
        stability.windows,
        stability.ahead,
        stability.compared,
    )
    return metrics.model_copy(update={"stability": stability})


def _notify(bot: Any, title: str, detail: str) -> None:
    if bot is None:
        return
    bot.send_card_sync("alert", {"title": title, "detail": detail, "service": "worker"})


__all__ = ["EXPIRY_REASON", "RemeasureReport", "expiry", "weekly_remeasure"]
