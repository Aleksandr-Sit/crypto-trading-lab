"""Ежедневное обновление свечей для ЖИВЫХ стратегий.

**Почему этого не было и чем это грозило.** Свечи в лаборатории собирались руками
(`lab data backfill`), и ни одно задание их не обновляло. Пока всё меряется на истории,
это незаметно: замер берёт что лежит. Но воскресное переизмерение считает «свежее окно»
по хранилищу, а не по календарю, и на несвежих данных оно молча меряет прошлое,
выдавая его за настоящее. 14.09.2026 хранилище отставало на неделю.

Обновляются только ряды, которые кому-то нужны: инструменты стратегий со ступени `paper`
и выше плюс сам бенчмарк (BTC). Качать весь архив каждую ночь незачем — у нас 1004
инструмента, а живых стратегий единицы.

Окно короткое (`days`), потому что бэкфилл идемпотентен и дыры закрывает перекрытием:
после любого простоя следующий запуск доберёт пропущенное сам.

**Фандинг — вместе со свечами (с 28.09.2026).** Воскресный перемер берёт ставку каждой
выплаты из хранилища, а при её отсутствии подставляет константу из параметров (0.01% за
8 часов). Для шорта это ДОХОД там, где у свежих листингов 2026 года реально −18% за месяц:
пропуск ставок льстил бы стратегии. Поэтому по бессрочным контрактам живых стратегий
ставки обновляются тем же окном, что и свечи.

**Время запуска — после закрытия суток UTC** (04:05 по Самаре). До 28.09 задание шло
в 23:50 UTC, за десять минут до закрытия дневного бара, и бар приезжал только следующей
ночью — журнал вперёд терял на этом каждое решение дневной стратегии.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lab.contracts import Rung, Status

log = logging.getLogger(__name__)

REFRESH_JOB = "data_refresh"
DEFAULT_DAYS = 14
# Ступени, с которых стратегия считается живой: ниже `paper` ей свежие данные не нужны —
# она меряется на истории и только.
LIVE_RUNGS = (Rung.PAPER, Rung.MICRO, Rung.SIGNAL, Rung.SEMI, Rung.AUTO)
LIVE_STATUSES = (Status.MEASURING.value, Status.PASSED.value)
BENCHMARK = "BTC/USDT"


@dataclass
class RefreshReport:
    rows: list[tuple[str, str, str, int, str | None]] = field(default_factory=list)
    # (площадка, инструмент, записано ставок, ошибка) — только бессрочные контракты
    funding: list[tuple[str, str, int, str | None]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(error is None for *_, error in self.rows) and all(
            error is None for *_, error in self.funding
        )

    def failed(self) -> list[str]:
        out = [f"{v} {s} {t}" for v, s, t, _, e in self.rows if e]
        out += [f"фандинг {v} {s}" for v, s, _, e in self.funding if e]
        return out

    def text(self) -> str:
        if not self.rows:
            return "Свечи: живых стратегий нет — обновлять нечего"
        good = [r for r in self.rows if r[4] is None]
        written = sum(r[3] for r in good)
        text = f"Свечи: рядов {len(self.rows)}, записано {written}"
        if self.funding:
            rates = sum(n for _, _, n, e in self.funding if e is None)
            text += f"; фандинг: рядов {len(self.funding)}, ставок {rates}"
        failed = self.failed()
        return text + (f", отказов {len(failed)}" if failed else "")


def targets(session: Any) -> set[tuple[str, str, str]]:
    """Какие ряды нужны живым стратегиям: (площадка, инструмент, таймфрейм)."""
    from lab.core.registry import Registry

    live_rungs = {r.value for r in LIVE_RUNGS}
    out: set[tuple[str, str, str]] = set()
    for row in Registry(session).list():
        if row.rung not in live_rungs or row.status not in LIVE_STATUSES:
            continue
        tf = row.timeframe or "1h"
        for instrument in row.instruments or []:
            if instrument and instrument != "*":
                out.add((row.venue, instrument, tf))
        # Бенчмарк нужен тому же замеру: без свежего BTC сравнение молча уезжает в прошлое.
        out.add((row.venue, BENCHMARK, tf))
    return out


def refresh(
    session_scope: Callable[[], Any],
    *,
    store: Any = None,
    funding_store: Any = None,
    root: str | None = None,
    days: int = DEFAULT_DAYS,
) -> RefreshReport:
    from lab.data import CandleStore
    from lab.data.backfill_cex import backfill_venue
    from lab.data.funding import FundingStore
    from lab.ops.measure import data_root

    store = store if store is not None else CandleStore(root or data_root())
    if funding_store is None:
        funding_store = FundingStore(root or data_root())
    report = RefreshReport()
    with session_scope() as session:
        wanted = targets(session)

    by_venue_tf: dict[tuple[str, str], list[str]] = {}
    for venue, instrument, tf in sorted(wanted):
        by_venue_tf.setdefault((venue, tf), []).append(instrument)

    for (venue, tf), symbols in by_venue_tf.items():
        try:
            results = backfill_venue(store, venue, symbols, tf, days)
        except Exception as err:  # noqa: BLE001 — площадка недоступна: не наша авария
            log.warning("Свечи %s %s: %s", venue, tf, err)
            for symbol in symbols:
                report.rows.append((venue, symbol, tf, 0, str(err)))
            continue
        for result in results:
            # Поля именно такие: `instrument` и `rows_written` (см. `data.backfill_cex`).
            # Ошибка в имени тут тихая — отчёт печатал бы «?» и ноль, а сбор шёл бы как шёл.
            report.rows.append(
                (venue, result.instrument, tf, int(result.rows_written or 0), result.error)
            )
            if result.error:
                log.warning("Свечи %s %s %s: %s", venue, result.instrument, tf, result.error)

    _refresh_funding(report, funding_store, wanted, days)
    return report


def _refresh_funding(
    report: RefreshReport, funding_store: Any, wanted: set[tuple[str, str, str]], days: int
) -> None:
    from lab.core.costs import is_perpetual
    from lab.data.backfill_cex import backfill_funding

    by_venue: dict[str, list[str]] = {}
    for venue, instrument, _tf in sorted(wanted):
        if is_perpetual(instrument) and instrument not in by_venue.get(venue, []):
            by_venue.setdefault(venue, []).append(instrument)
    for venue, symbols in by_venue.items():
        try:
            results = backfill_funding(funding_store, venue, symbols, days)
        except Exception as err:  # noqa: BLE001 — площадка недоступна: не наша авария
            log.warning("Фандинг %s: %s", venue, err)
            report.funding.extend((venue, s, 0, str(err)) for s in symbols)
            continue
        report.funding.extend((venue, s, int(n or 0), e) for s, n, e in results)


def data_refresh_job(
    session_scope: Callable[[], Any],
    *,
    store: Any = None,
    root: str | None = None,
    days: int = DEFAULT_DAYS,
    alert: Callable[[str, dict[str, Any]], Any] | None = None,
):
    """Задание `data_refresh` (см. `config/schedule.yaml`)."""
    from datetime import UTC, datetime

    from lab.ops.scheduler import Job

    def run() -> RefreshReport:
        report = refresh(session_scope, store=store, root=root, days=days)
        log.info("%s", report.text())
        if not report.ok and alert is not None:
            failed = report.failed()
            alert(
                "alert",
                {
                    "service": "data_refresh",
                    "detail": "Свечи не обновлены: " + ", ".join(failed[:5]),
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        return report

    return Job(
        id=REFRESH_JOB,
        func=run,
        description="Свежие свечи для живых стратегий (иначе замер молча меряет прошлое)",
    )


__all__ = ["REFRESH_JOB", "RefreshReport", "data_refresh_job", "refresh", "targets"]
