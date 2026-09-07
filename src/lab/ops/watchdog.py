"""Watchdog (R32i.2, история 104): сервис не подал признаков жизни 5 минут → карточка `alert`.

Каждый сервис (`worker`, `bot`, `web`) на каждом тике пишет heartbeat в базу — общий
для всех процессов признак жизни, в отличие от файла в контейнере. Watchdog живёт в
worker'е, раз в минуту сверяет отметки и шлёт `alert` один раз на один простой:
повторная тишина того же сервиса не превращается в поток сообщений, а возвращение
heartbeat снимает взвод.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, String, select
from sqlalchemy.orm import Mapped, mapped_column

from lab.db.base import Base, utcnow

log = logging.getLogger(__name__)

SILENCE_S = 300
WATCHDOG_JOB = "watchdog"
WATCHDOG_CRON = "* * * * *"
SERVICES = ("worker", "bot", "web")


class ServiceHeartbeatRow(Base):
    """Отметка жизни сервиса (миграция 0011)."""

    __tablename__ = "service_heartbeats"

    service: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    detail: Mapped[str] = mapped_column(String(300), default="")
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


@dataclass(frozen=True)
class ServiceState:
    service: str
    last_seen: datetime
    silent_for_s: int
    alive: bool


def beat(session: Any, service: str, *, now: datetime | None = None, detail: str = "") -> None:
    """Отметить, что сервис жив. Зовётся из цикла сервиса и из heartbeat-задания."""
    at = now or datetime.now(UTC)
    row = session.get(ServiceHeartbeatRow, service)
    if row is None:
        row = ServiceHeartbeatRow(service=service)
        session.add(row)
    row.ts, row.detail, row.alerted_at = at, detail, None


class Watchdog:
    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        alert: Callable[[str, dict[str, Any]], Any] | None = None,
        silence_s: int = SILENCE_S,
        services: tuple[str, ...] | None = None,
    ) -> None:
        self._sf = session_factory  # фабрика контекста сессии: `with session_factory() as s`
        self._alert = alert
        self.silence_s = silence_s
        self.services = services

    def states(self, *, now: datetime | None = None) -> list[ServiceState]:
        at = now or datetime.now(UTC)
        with self._sf() as session:
            rows = session.scalars(select(ServiceHeartbeatRow)).all()
            return [self._state(row, at) for row in rows if self._watched(row.service)]

    def _watched(self, service: str) -> bool:
        return self.services is None or service in self.services

    def _state(self, row: ServiceHeartbeatRow, at: datetime) -> ServiceState:
        last = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=UTC)
        silent = int((at - last).total_seconds())
        return ServiceState(row.service, last, silent, silent <= self.silence_s)

    def beat(self, service: str, *, now: datetime | None = None, detail: str = "") -> None:
        with self._sf() as session:
            beat(session, service, now=now, detail=detail)

    def check(self, *, now: datetime | None = None) -> list[ServiceState]:
        """Задание `watchdog`: вернуть молчащие сервисы и разослать по ним `alert` (по разу)."""
        at = now or datetime.now(UTC)
        dead: list[ServiceState] = []
        with self._sf() as session:
            for row in session.scalars(select(ServiceHeartbeatRow)).all():
                if not self._watched(row.service):
                    continue
                state = self._state(row, at)
                if state.alive:
                    continue
                dead.append(state)
                if row.alerted_at is not None:
                    continue
                row.alerted_at = at
                self._send(state)
        return dead

    def _send(self, state: ServiceState) -> None:
        minutes = state.silent_for_s // 60
        log.error("Сервис %s молчит %s мин", state.service, minutes)
        if self._alert is None:
            return
        self._alert(
            "alert",
            {
                "service": state.service,
                "silent_for_s": state.silent_for_s,
                "detail": (
                    f"Сервис {state.service} не подавал признаков жизни {minutes} мин "
                    f"(последний heartbeat {state.last_seen:%d.%m %H:%M} UTC)"
                ),
                "at": datetime.now(UTC).isoformat(),
            },
        )

    def job(self, *, cron: str = WATCHDOG_CRON):
        from lab.ops.scheduler import Job

        return Job(
            id=WATCHDOG_JOB,
            func=self.check,
            cron=cron,
            description="Сервис не жив 5 минут → alert (R32i.2)",
        )

    def heartbeat_job(self, service: str, *, cron: str = WATCHDOG_CRON):
        from lab.ops.scheduler import Job

        return Job(
            id=f"heartbeat_{service}",
            func=lambda: self.beat(service),
            cron=cron,
            description=f"Heartbeat сервиса {service}",
        )


def silence(state: ServiceState) -> timedelta:
    return timedelta(seconds=state.silent_for_s)


__all__ = [
    "SERVICES",
    "SILENCE_S",
    "WATCHDOG_CRON",
    "WATCHDOG_JOB",
    "ServiceHeartbeatRow",
    "ServiceState",
    "Watchdog",
    "beat",
    "silence",
]
