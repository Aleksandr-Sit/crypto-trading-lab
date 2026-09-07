"""Задания планировщика таска 12: поиск, переизмерение, срок годности, перелив, G10.

Расписание — `config/schedule.yaml` (TZ Europe/Samara, решение §11): `discovery` пн 06:00,
`remeasure` вс 22:00, `expiry` вс 22:15, `rebalance_proposal` вс 22:30. Зависимости
(замер, лестница, риск-ядро, бот) подставляет worker — здесь только связывание.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from lab.discovery import Discovery, ScanResult
from lab.discovery.config import DiscoveryConfig, load_discovery
from lab.ops.jobs.models import RebalanceProposalRow
from lab.ops.jobs.rebalance import (
    Proposal,
    ProposalNotFound,
    confirm_rebalance,
    proposals,
    rebalance_hook,
    rebalance_proposal,
)
from lab.ops.jobs.remeasure import RemeasureReport, expiry, weekly_remeasure
from lab.ops.jobs.reminder import SeedReminder
from lab.ops.scheduler import Job

SCAN_JOB = "discovery"
REMEASURE_JOB = "remeasure"
EXPIRY_JOB = "expiry"
REBALANCE_JOB = "rebalance_proposal"
REMINDER_JOB = "seed_reminder"


def weekly_scan(
    session_scope: Callable[[], Any],
    *,
    sources: Sequence[Any] | None = None,
    bot: Any = None,
    config: DiscoveryConfig | None = None,
    now: datetime | None = None,
) -> ScanResult:
    """Еженедельный поиск кандидатов (R15): обход лент, очередь, карточки в телегу."""
    cfg = config or load_discovery()
    with session_scope() as session:
        discovery = Discovery(session, sources=sources, bot=bot, config=cfg)
        return discovery.scan(now=now)


def jobs(
    session_scope: Callable[[], Any],
    *,
    bot: Any = None,
    sources: Sequence[Any] | None = None,
    measure: Callable[..., Any] | None = None,
    ladder_factory: Callable[[Any], Any] | None = None,
    risk: Any = None,
    reminder: SeedReminder | None = None,
    config: DiscoveryConfig | None = None,
) -> list[Job]:
    """Задания для `ops.scheduler.register` — те, для которых есть зависимости."""
    cfg = config or load_discovery()
    out = [
        Job(
            id=SCAN_JOB,
            func=lambda: weekly_scan(session_scope, sources=sources, bot=bot, config=cfg),
            description="Еженедельный поиск кандидатов (R15)",
        )
    ]
    if measure is not None and ladder_factory is not None:
        out.append(
            Job(
                id=REMEASURE_JOB,
                func=lambda: weekly_remeasure(
                    session_scope,
                    measure=measure,
                    ladder_factory=ladder_factory,
                    bot=bot,
                    config=cfg,
                ),
                description="Еженедельное переизмерение живых стратегий (R14)",
            )
        )
    if ladder_factory is not None:
        out.append(
            Job(
                id=EXPIRY_JOB,
                func=lambda: expiry(
                    session_scope, ladder_factory=ladder_factory, bot=bot, config=cfg
                ),
                description="Срок годности стратегий (R14.1)",
            )
        )
    if risk is not None:
        out.append(
            Job(
                id=REBALANCE_JOB,
                func=lambda: rebalance_proposal(
                    session_scope, risk=risk, bot=bot, config=cfg
                ),
                description="Предложение перелива излишка рисковых веток (G05)",
            )
        )
    if reminder is not None:
        out.append(reminder.job())
    return out


__all__ = [
    "EXPIRY_JOB",
    "REBALANCE_JOB",
    "REMEASURE_JOB",
    "REMINDER_JOB",
    "SCAN_JOB",
    "Proposal",
    "ProposalNotFound",
    "RebalanceProposalRow",
    "RemeasureReport",
    "SeedReminder",
    "confirm_rebalance",
    "expiry",
    "jobs",
    "proposals",
    "rebalance_hook",
    "rebalance_proposal",
    "weekly_remeasure",
    "weekly_scan",
]
