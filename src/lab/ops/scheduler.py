"""Планировщик (решение §11): APScheduler в `worker`, расписание из `config/schedule.yaml`.

Задания регистрируют другие модули: `register(Job("morning_report", fn))` — cron и TZ
берутся из конфига по id задания; своё расписание можно задать явно через `Job(cron=...)`.
Регистрация идемпотентна: повторный `register` с тем же id заменяет задание, поэтому
перезапуск процесса не плодит дублей. Одно задание никогда не идёт в два экземпляра
(`max_instances=1`), пропущенные срабатывания схлопываются (`coalesce=True`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from lab.config import load_schedule
from lab.config.models import ScheduleConfig

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Europe/Samara"
MISFIRE_GRACE_S = 3600


@dataclass
class Job:
    """Задание планировщика. `cron` — 5 полей; None → взять из `schedule.yaml` по `id`."""

    id: str
    func: Callable[..., Any]
    cron: str | None = None
    description: str = ""
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)


class Scheduler:
    def __init__(self, config: ScheduleConfig | None = None, *, timezone: str | None = None):
        self.config = config or load_schedule()
        self.tz = ZoneInfo(timezone or self.config.timezone or DEFAULT_TIMEZONE)
        self._aps = BackgroundScheduler(
            timezone=self.tz,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": MISFIRE_GRACE_S,
            },
        )
        self._jobs: dict[str, Job] = {}

    # -- регистрация ---------------------------------------------------------------------

    def register(self, job: Job) -> Job:
        """Зарегистрировать задание. Без `cron` — расписание по `schedule.yaml`;
        задание с `enabled: false` в конфиге не ставится (но регистрация запоминается)."""
        cron = job.cron
        if cron is None:
            spec = self.config.jobs.get(job.id)
            if spec is None:
                raise KeyError(f"задание {job.id!r} не описано в schedule.yaml и без cron")
            if not spec.enabled:
                log.info("Задание %s выключено в schedule.yaml — не ставится", job.id)
                self._forget(job.id)
                return job
            cron = spec.cron
            job.description = job.description or spec.description
        self._forget(job.id)  # до старта replace_existing не чистит очередь ожидающих
        self._aps.add_job(
            job.func,
            CronTrigger.from_crontab(cron, timezone=self.tz),
            id=job.id,
            name=job.description or job.id,
            args=job.args,
            kwargs=job.kwargs,
            replace_existing=True,
        )
        self._jobs[job.id] = job
        log.info("Задание %s: cron=%s tz=%s", job.id, cron, self.tz.key)
        return job

    def _forget(self, job_id: str) -> None:
        if self._aps.get_job(job_id) is not None:
            self._aps.remove_job(job_id)
        self._jobs.pop(job_id, None)

    def jobs(self) -> list[str]:
        return [j.id for j in self._aps.get_jobs()]

    def next_run(self, job_id: str, *, after: datetime | None = None) -> datetime | None:
        job = self._aps.get_job(job_id)
        if job is None:
            return None
        if after is None:
            return job.next_run_time
        return job.trigger.get_next_fire_time(None, after.astimezone(self.tz))

    # -- запуск --------------------------------------------------------------------------

    def start(self) -> None:
        if not self._aps.running:
            self._aps.start()

    def shutdown(self) -> None:
        if self._aps.running:
            self._aps.shutdown(wait=False)

    @property
    def running(self) -> bool:
        return bool(self._aps.running)

    def run_now(self, job_id: str) -> Any:
        """Выполнить задание синхронно в текущем потоке (для CLI `--once` и тестов)."""
        job = self._jobs[job_id]
        return job.func(*job.args, **job.kwargs)

    def fire(self, job_id: str) -> None:
        """Попросить APScheduler выполнить задание при ближайшем тике (в его потоке)."""
        self._aps.modify_job(
            job_id, next_run_time=datetime.now(self.tz) + timedelta(milliseconds=10)
        )
        self._aps.wakeup()


_default: Scheduler | None = None


def default_scheduler() -> Scheduler:
    global _default
    if _default is None:
        _default = Scheduler()
    return _default


def register(job: Job) -> Job:
    """Модульный шов из спецификации: `ops.scheduler.register(job)`."""
    return default_scheduler().register(job)
