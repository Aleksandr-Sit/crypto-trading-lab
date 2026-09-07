"""Суточная сверка журнала с площадками (R31i.1, история 91; расписание — 04:00).

Для каждой площадки берём филлы за окно (`executor.fills(since)`) и сравниваем с журналом
(`core.journal.Journal.reconcile`). Любое расхождение — карточка `alert` оператору;
журнал при этом ничего не правит: расхождение разбирает человек.

Недоступная площадка не срывает сверку остальных: её строка уходит в `alert` отдельно.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from lab.core.journal import Journal, ReconcileReport

log = logging.getLogger(__name__)

RECONCILE_JOB = "reconcile"
WINDOW = timedelta(days=1)


def reconcile_all(
    session_scope: Callable[[], Any],
    *,
    executors: Mapping[str, Any],
    alert: Callable[[str, dict[str, Any]], Any] | None = None,
    since: datetime | None = None,
    now: datetime | None = None,
    window: timedelta = WINDOW,
    tolerance: Decimal = Decimal("0.0001"),
) -> list[ReconcileReport]:
    at = now or datetime.now(UTC)
    start = since or at - window
    reports: list[ReconcileReport] = []
    for venue, executor in executors.items():
        try:
            fills = list(executor.fills(start))
        except Exception as err:  # noqa: BLE001 — площадка недоступна: скажем и пойдём дальше
            log.warning("Сверка %s: площадка недоступна: %s", venue, err)
            _alert(alert, venue, f"{venue}: сверка не выполнена — площадка недоступна ({err})", at)
            continue
        with session_scope() as session:
            report = Journal(session).reconcile(
                venue, fills, since=start, tolerance=tolerance, now=at
            )
        reports.append(report)
        if report.ok:
            log.info("Сверка %s: расхождений нет (%s филлов)", venue, report.checked)
            continue
        lines = "; ".join(str(m) for m in report.mismatches[:5])
        _alert(
            alert,
            venue,
            f"{venue}: расхождений {len(report.mismatches)} из {report.checked} филлов. {lines}",
            at,
        )
    return reports


def _alert(
    alert: Callable[[str, dict[str, Any]], Any] | None, venue: str, detail: str, at: datetime
) -> None:
    log.error("Сверка: %s", detail)
    if alert is None:
        return
    alert(
        "alert",
        {
            "service": f"reconcile:{venue}",
            "title": "Расхождение журнала с площадкой",
            "detail": detail,
            "at": at.isoformat(),
        },
    )


def reconcile_job(
    session_scope: Callable[[], Any],
    *,
    executors: Mapping[str, Any],
    alert: Callable[[str, dict[str, Any]], Any] | None = None,
):
    """Задание `reconcile` (04:00 по `schedule.yaml`)."""
    from lab.ops.scheduler import Job

    return Job(
        id=RECONCILE_JOB,
        func=lambda: reconcile_all(session_scope, executors=executors, alert=alert),
        description="Сверка журнала с площадками (R31i.1)",
    )


__all__ = ["RECONCILE_JOB", "WINDOW", "reconcile_all", "reconcile_job"]
