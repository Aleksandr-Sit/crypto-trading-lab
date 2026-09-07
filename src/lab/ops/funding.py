"""Платежи фандинга в журнал (R29i, отклонение таска 04: отдельной таблицы фандинга нет).

`executors.cex.funding_payments(since)` отдаёт списания/начисления площадки; здесь они
раскладываются по открытым сделкам того же инструмента (пропорционально объёму) в колонку
`trades.funding` — ту самую, что уходит в `pnl_net` при закрытии сделки. Знак: площадка
списала (`amount < 0`) → для нас издержка `+`, начислила → издержка со знаком `−`.

Повтор задания не задваивает платёж: применённые id платежей помнятся в `system_flags`.
Платёж без открытой позиции пропускается (позиция уже закрыта — учитывать некуда).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from lab.db.models import StrategyRow, SystemFlagRow, TradeRow

log = logging.getLogger(__name__)

FUNDING_JOB = "funding"
FLAG_PREFIX = "funding_applied:"
KEEP_IDS = 500


@dataclass
class FundingReport:
    venue: str
    payments: int = 0
    applied: int = 0
    skipped: int = 0
    total_usd: Decimal = Decimal(0)
    ids: list[str] = field(default_factory=list)


def _flag(session: Any, venue: str) -> SystemFlagRow:
    key = f"{FLAG_PREFIX}{venue}"
    row = session.get(SystemFlagRow, key)
    if row is None:
        row = SystemFlagRow(key=key, value={"ids": []}, updated_by="worker")
        session.add(row)
    return row


def record_funding(
    session: Any,
    *,
    executor: Any,
    venue: str,
    since: datetime,
    mode: str = "live",
    now: datetime | None = None,
) -> FundingReport:
    """Списать фандинг площадки на открытые сделки журнала."""
    at = now or datetime.now(UTC)
    report = FundingReport(venue=venue)
    try:
        payments = list(executor.funding_payments(since))
    except Exception as err:  # noqa: BLE001 — площадка недоступна: не наша авария
        log.warning("Фандинг %s: площадка недоступна: %s", venue, err)
        return report

    flag = _flag(session, venue)
    seen: list[str] = list(flag.value.get("ids", []))
    report.payments = len(payments)

    for payment in payments:
        if payment.id in seen:
            continue
        rows = session.scalars(
            select(TradeRow)
            .where(
                TradeRow.instrument == payment.instrument,
                TradeRow.venue == venue,
                TradeRow.mode == mode,
                TradeRow.closed_at.is_(None),
            )
            .order_by(TradeRow.opened_at, TradeRow.id)
        ).all()
        total_qty = sum((r.qty for r in rows), Decimal(0))
        if not rows or total_qty <= 0:
            report.skipped += 1
            log.info(
                "Фандинг %s %s: нет открытой позиции — платёж %s пропущен",
                venue,
                payment.instrument,
                payment.id,
            )
            continue
        cost = -Decimal(payment.amount)
        for row in rows:
            row.funding = row.funding + cost * (row.qty / total_qty)
        seen.append(payment.id)
        report.applied += 1
        report.total_usd += cost
        report.ids.append(payment.id)

    flag.value = {"ids": seen[-KEEP_IDS:]}
    flag.updated_at = at
    session.flush()
    if report.applied:
        log.info(
            "Фандинг %s: применено %s платежей на %s USD",
            venue,
            report.applied,
            report.total_usd,
        )
    return report


def record_funding_all(
    session_scope: Callable[[], Any],
    *,
    executors: dict[str, Any],
    since: datetime | None = None,
    hours: int = 24,
    now: datetime | None = None,
) -> list[FundingReport]:
    from datetime import timedelta

    at = now or datetime.now(UTC)
    start = since or at - timedelta(hours=hours)
    out: list[FundingReport] = []
    for venue, executor in executors.items():
        if not hasattr(executor, "funding_payments"):
            continue
        with session_scope() as session:
            out.append(record_funding(session, executor=executor, venue=venue, since=start, now=at))
    return out


def funding_job(
    session_scope: Callable[[], Any], *, executors: dict[str, Any], cron: str = "5 * * * *"
):
    """Ежечасное задание: фандинг площадок → журнал."""
    from lab.ops.scheduler import Job

    return Job(
        id=FUNDING_JOB,
        func=lambda: record_funding_all(session_scope, executors=executors),
        cron=cron,
        description="Платежи фандинга площадок в журнал (R29i)",
    )


def strategies_of_venue(session: Any, venue: str) -> list[str]:
    return list(session.scalars(select(StrategyRow.id).where(StrategyRow.venue == venue)).all())


__all__ = [
    "FUNDING_JOB",
    "FundingReport",
    "funding_job",
    "record_funding",
    "record_funding_all",
    "strategies_of_venue",
]
