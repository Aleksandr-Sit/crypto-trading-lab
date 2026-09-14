"""Журнал решений ВПЕРЁД: живые стратегии прогоняются по свежим барам, решения пишутся с датой.

**Зачем.** До сегодня ступень `paper` не создавала ни одной живой записи: в worker не было
задания, которое кормит стратегии барами. Замер `--mode forward` читает журнал и увидел бы
пустоту — при этом снаружи всё «работало». Без этого журнала нельзя проверить вперёд ни одну
стратегию и ни один чужой сигнал (каналы Telegram — тот же механизм: запись в момент
получения, сверка через квартал).

**Главное правило и почему оно такое.** На ПЕРВОМ запуске не записывается ничего: ставится
только водяной знак «с этого момента считаем». Иначе прогон по истории вывалил бы в журнал
сотни решений задним числом, и они выглядели бы как сделанные вовремя прогнозы. Проверка
вперёд, начатая записью прошлого, — не проверка вперёд.

**Состояние не хранится, а восстанавливается.** Стратегии нужны свои недели истории
(ротации — двенадцать), и хранить её между запусками значило бы завести вторую копию
правды, которая разъедется с первой. Вместо этого каждый запуск проигрывает разогревочное
окно заново: результат детерминирован и повторим, а лишний счёт на дневных барах ничего
не стоит.

Ордеров задание не создаёт вовсе: ниже ступени `micro` реальных ордеров нет по устройству
лестницы, а здесь нет даже пути к ним — только `Journal.record_signal`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from lab.contracts import Candle, Signal, parse_tf
from lab.ops.jobs.data_refresh import LIVE_RUNGS, LIVE_STATUSES

log = logging.getLogger(__name__)

FORWARD_JOB = "forward_journal"
FLAG_PREFIX = "forward_seen:"
# Сколько баров проигрывать для разогрева состояния. 800 дневных — больше двух лет:
# хватает и годовому якорю накопительной лестницы, и окнам импульса ротации.
WARMUP_BARS = 800


@dataclass
class ForwardReport:
    journaled: list[tuple[str, str, str, datetime]] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def text(self) -> str:
        parts = [f"записано решений {len(self.journaled)}"]
        if self.started:
            parts.append(f"начат отсчёт у {len(self.started)}")
        if self.skipped:
            parts.append(f"пропущено {len(self.skipped)}")
        return "Журнал вперёд: " + ", ".join(parts)


def _flag(session: Any, strategy_id: str) -> Any:
    from lab.db.models import SystemFlagRow

    key = f"{FLAG_PREFIX}{strategy_id}"
    row = session.get(SystemFlagRow, key)
    if row is None:
        row = SystemFlagRow(key=key, value={}, updated_by="worker")
        session.add(row)
    return row


def _watermark(row: Any) -> datetime | None:
    raw = (row.value or {}).get("at")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def replay(strategy: Any, bars: Sequence[Candle]) -> list[Signal]:
    """Проиграть стратегию по барам в порядке времени и собрать её решения.

    Бары сортируются по времени ПО ВСЕМ инструментам сразу: связка из двух ног обязана
    видеть их в том же порядке, в каком они приходили бы вживую. Иначе повторяется
    старая ловушка — решение принимается по ценам разных моментов.
    """
    strategy.reset()
    out: list[Signal] = []
    for bar in sorted(bars, key=lambda b: (b.ts, b.instrument)):
        try:
            out.extend(strategy.on_bar(bar) or [])
        except Exception as err:  # noqa: BLE001 — дефект одного правила не валит остальные
            log.warning("Стратегия %s упала на баре %s: %s", strategy.strategy_id, bar.ts, err)
            break
    return out


def _bars(store: Any, record: Any, tf: str, now: datetime) -> list[Candle]:
    from lab.ops.measure import _from_store

    step = parse_tf(tf) or timedelta(days=1)
    window = (now - step * WARMUP_BARS, now + step)
    out: list[Candle] = []
    for instrument in record.instruments or []:
        if not instrument or instrument == "*":
            continue
        out.extend(_from_store(store, record.venue, instrument, tf, window))
    return out


def run_forward(
    session_scope: Callable[[], Any],
    *,
    store: Any = None,
    root: str | None = None,
    now: datetime | None = None,
) -> ForwardReport:
    from lab.core.journal import Journal
    from lab.core.registry import Registry
    from lab.data import CandleStore
    from lab.ops.measure import _build_strategy, data_root

    store = store if store is not None else CandleStore(root or data_root())
    at = now or datetime.now(UTC)
    report = ForwardReport()
    live_rungs = {r.value for r in LIVE_RUNGS}

    with session_scope() as session:
        records = [
            row
            for row in Registry(session).list()
            if row.rung in live_rungs and row.status in LIVE_STATUSES
        ]
        journal = Journal(session)
        for record in records:
            try:
                strategy = _build_strategy(record)
            except Exception as err:  # noqa: BLE001 — нет кода правил: не наша авария
                report.skipped.append((record.id, f"нет кода правил: {err}"))
                continue
            if strategy is None:
                report.skipped.append((record.id, "нет кода правил"))
                continue

            flag = _flag(session, record.id)
            seen = _watermark(flag)
            tf = record.timeframe or "1h"
            bars = _bars(store, record, tf, at)
            if not bars:
                report.skipped.append((record.id, "нет свечей за окно"))
                continue

            signals = replay(strategy, bars)
            if seen is None:
                # Первый запуск: только ставим отметку. Записать сейчас решения из разогрева
                # значило бы задним числом создать «прогнозы», которых никто не делал.
                flag.value = {"at": at.isoformat()}
                flag.updated_by = "worker"
                report.started.append(record.id)
                continue

            fresh = [s for s in signals if s.decided_at > seen]
            for signal in fresh:
                signal.meta.setdefault("source", "forward_journal")
                # Когда решение попало в журнал — отдельно от того, когда оно принято.
                # Расхождение видно глазами: после простоя записи приезжают поздно.
                signal.meta.setdefault("seen_at", at.isoformat())
                journal.record_signal(signal)
                report.journaled.append(
                    (record.id, signal.instrument, signal.side, signal.decided_at)
                )
            flag.value = {"at": at.isoformat()}
            flag.updated_by = "worker"
    return report


def forward_job(
    session_scope: Callable[[], Any],
    *,
    store: Any = None,
    root: str | None = None,
    alert: Callable[[str, dict[str, Any]], Any] | None = None,
):
    """Задание `forward_journal` (см. `config/schedule.yaml`)."""
    from lab.ops.scheduler import Job

    def run() -> ForwardReport:
        report = run_forward(session_scope, store=store, root=root)
        log.info("%s", report.text())
        for strategy_id, reason in report.skipped:
            log.info("Журнал вперёд, пропуск %s: %s", strategy_id, reason)
        if report.journaled and alert is not None:
            alert(
                "alert",
                {
                    "service": "forward_journal",
                    "detail": "Решения вперёд: "
                    + ", ".join(f"{s} {i} {side}" for s, i, side, _ in report.journaled[:5]),
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        return report

    return Job(
        id=FORWARD_JOB,
        func=run,
        description="Решения живых стратегий в журнал с датой — основа проверки вперёд",
    )


__all__ = [
    "FLAG_PREFIX",
    "FORWARD_JOB",
    "WARMUP_BARS",
    "ForwardReport",
    "forward_job",
    "replay",
    "run_forward",
]
