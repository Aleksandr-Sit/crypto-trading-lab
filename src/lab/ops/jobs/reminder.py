"""Одноразовое напоминание G10: «дополни список кандидатов своими именами».

Взводится после первого успешного `/status` (первый запуск состоялся), срабатывает
ровно один раз — состояние живёт в `system_flags`, поэтому рестарт его не сбрасывает.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from lab.db.base import utcnow
from lab.db.models import SystemFlagRow
from lab.ops.scheduler import Job

log = logging.getLogger(__name__)

FLAG = "discovery:seed_reminder"
TITLE = "Дополни список кандидатов"
TEXT = (
    "Сборка прошла первый запуск. Скинь имена, каналы и кошельки, которые считаешь "
    "стоящими: они лягут в candidates/seed.md и в очередь кандидатов рядом с тем, "
    "что нашёл поиск (G10)."
)


class SeedReminder:
    def __init__(
        self,
        session_scope: Callable[[], Any],
        *,
        bot: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session_scope = session_scope
        self.bot = bot
        self._clock = clock or utcnow

    # -- состояние ------------------------------------------------------------------

    def _flag(self, session: Any) -> SystemFlagRow:
        row = session.get(SystemFlagRow, FLAG)
        if row is None:
            row = SystemFlagRow(key=FLAG, value={})
            session.add(row)
            session.flush()
        return row

    def state(self) -> dict[str, Any]:
        with self.session_scope() as session:
            return dict(self._flag(session).value or {})

    def arm(self, *, now: datetime | None = None) -> bool:
        """Первый успешный `/status` — взвести напоминание (повтор ничего не меняет)."""
        at = now or self._clock()
        with self.session_scope() as session:
            row = self._flag(session)
            value = dict(row.value or {})
            if value.get("armed_at") or value.get("sent_at"):
                return False
            row.value = {**value, "armed_at": at.isoformat()}
            session.flush()
        log.info("Напоминание G10 взведено")
        return True

    def run(self, *, now: datetime | None = None) -> bool:
        """Задание планировщика: отправить напоминание, если взведено и ещё не отправлено."""
        at = now or self._clock()
        with self.session_scope() as session:
            row = self._flag(session)
            value = dict(row.value or {})
            if not value.get("armed_at") or value.get("sent_at"):
                return False
            row.value = {**value, "sent_at": at.isoformat()}
            session.flush()
        if self.bot is not None:
            self.bot.send_card_sync(
                "alert", {"title": TITLE, "detail": TEXT, "service": "worker"}
            )
        log.info("Напоминание G10 отправлено")
        return True

    # -- планировщик ----------------------------------------------------------------

    def job(self, cron: str | None = None) -> Job:
        return Job(
            id="seed_reminder",
            func=self.run,
            cron=cron,
            description="Одноразовое напоминание G10: дополни список кандидатов",
        )

    def status_hook(self) -> Callable[[], None]:
        """Хук для бота: зовётся после первого успешного `/status` (подключает T14)."""

        def hook() -> None:
            self.arm()

        return hook


__all__ = ["FLAG", "TEXT", "TITLE", "SeedReminder"]
