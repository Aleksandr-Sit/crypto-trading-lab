"""ops.scheduler: register(job) по schedule.yaml, идемпотентность при перезапуске, срабатывание."""

import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from lab.config import load_schedule
from lab.ops.scheduler import Job, Scheduler

SAMARA = ZoneInfo("Europe/Samara")


@pytest.fixture
def scheduler():
    s = Scheduler(load_schedule())
    yield s
    s.shutdown()


def test_register_takes_cron_from_schedule_yaml_in_samara_tz(scheduler: Scheduler) -> None:
    scheduler.register(Job("morning_report", lambda: None))
    # следующий запуск после 5 сентября 2026 12:00 по Самаре — 6 сентября 09:00 по Самаре
    now = datetime(2026, 9, 5, 12, 0, tzinfo=SAMARA)
    nxt = scheduler.next_run("morning_report", after=now)
    assert nxt == datetime(2026, 9, 6, 9, 0, tzinfo=SAMARA)
    assert nxt.utcoffset().total_seconds() == 4 * 3600  # Самара = UTC+4


def test_register_is_idempotent_on_restart(scheduler: Scheduler) -> None:
    calls: list[str] = []
    scheduler.register(Job("backup", lambda: calls.append("v1")))
    scheduler.register(Job("backup", lambda: calls.append("v2")))  # «перезапуск» модуля
    assert scheduler.jobs() == ["backup"]
    scheduler.run_now("backup")
    assert calls == ["v2"]


def test_unknown_job_without_cron_is_rejected(scheduler: Scheduler) -> None:
    with pytest.raises(KeyError):
        scheduler.register(Job("no_such_job", lambda: None))


def test_disabled_job_is_not_scheduled() -> None:
    cfg = load_schedule().model_copy(deep=True)
    cfg.jobs["backup"].enabled = False
    s = Scheduler(cfg)
    try:
        s.register(Job("backup", lambda: None))
        assert s.jobs() == []
    finally:
        s.shutdown()


def test_job_fires_through_apscheduler(scheduler: Scheduler) -> None:
    fired = threading.Event()
    scheduler.register(Job("tick", fired.set, cron="* * * * *"))
    scheduler.start()
    scheduler.fire("tick")
    assert fired.wait(5), "задание не сработало за 5 с"
