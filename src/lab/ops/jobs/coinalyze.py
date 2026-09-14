"""Ежедневный сбор ликвидаций и открытого интереса (Coinalyze) — чтобы не терять историю.

У источника окно ПЛЫВЁТ: держится около 1500 суточных точек, и самый старый день каждый
день выпадает. Сегодня доступен август 2022, через год — август 2023.

Зачем это нужно при отвергнутой гипотезе. Проверка 14.09.2026 показала, что связь
«крупные ликвидации → движение вперёд» на четырёх годах от шума не отличается ни в одной
из 75 клеток сетки — но знак положителен во ВСЕХ клетках, а случайность дала бы половину
отрицательных. Различить это можно только на большем числе наблюдений; не собирая сейчас,
мы лишим себя такой возможности навсегда.

Устройство то же, что у сборщика CryptoQuant: окно берётся целиком (перекрытие закрывает
дыру после любого простоя), запись идемпотентна, текущие незакрытые сутки не пишутся.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lab.data.daily_market import CoinalyzeStore
from lab.feeds.coinalyze import CoinalyzeConfig, CoinalyzeFeed, load_coinalyze, make_feed
from lab.ops.jobs.cryptoquant import data_root

log = logging.getLogger(__name__)

COINALYZE_JOB = "coinalyze"
# Складываем под псевдоплощадкой: ряд — это СУММА по биржам, а не отдельная биржа,
# и называть его именем одной из них значило бы соврать.
AGGREGATE = "all_exchange"


@dataclass
class CollectResult:
    series: int = 0
    rows: int = 0
    written: int = 0
    skipped: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def __str__(self) -> str:
        if self.error:
            return f"Coinalyze: не собрано ({self.error})"
        tail = f", пропусков {len(self.skipped)}" if self.skipped else ""
        return (
            f"Coinalyze: рядов {self.series}, суток {self.rows}, "
            f"новых строк {self.written}{tail}"
        )


def collect(
    *,
    feed: CoinalyzeFeed | None = None,
    store: CoinalyzeStore | None = None,
    config: CoinalyzeConfig | None = None,
    now: datetime | None = None,
) -> CollectResult:
    cfg = config or load_coinalyze()
    src = feed if feed is not None else make_feed(pause_s=cfg.pause_s)
    if not src.enabled:
        return CollectResult(error="COINALYZE_API_KEY не задан")
    dst = store or CoinalyzeStore(data_root())
    at = now or datetime.now(UTC)
    result = CollectResult()

    for asset, symbols in cfg.markets.items():
        if not symbols:
            continue
        rows, notes = src.collect(list(symbols))
        result.skipped.extend(notes)
        closed = [r for r in rows if r.ts.date() < at.date()]
        if not closed:
            continue
        result.series += 1
        result.rows += len(closed)
        result.written += dst.write(asset, AGGREGATE, closed)

    for note in result.skipped:
        log.info("Coinalyze пропуск: %s", note)
    return result


def coinalyze_job(
    *,
    feed: CoinalyzeFeed | None = None,
    store: CoinalyzeStore | None = None,
    config: CoinalyzeConfig | None = None,
    alert: Callable[[str, dict[str, Any]], Any] | None = None,
):
    """Задание `coinalyze` (см. `config/schedule.yaml`).

    Отсутствие ключа — не авария: источник просто спит, как и любой другой без ключа.
    Карточка уходит, только когда ключ есть, а сбор не удался: это единственный случай,
    когда теряются сутки, которых потом не вернуть.
    """
    from lab.ops.scheduler import Job

    def run() -> CollectResult:
        result = collect(feed=feed, store=store, config=config)
        if result.error and "не задан" not in result.error and alert is not None:
            alert(
                "alert",
                {
                    "service": "coinalyze",
                    "detail": f"Ликвидации не собраны: {result.error}",
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        log.info("%s", result)
        return result

    return Job(
        id=COINALYZE_JOB,
        func=run,
        description="Ликвидации и открытый интерес (Coinalyze) — окно источника плывёт",
    )


__all__ = ["AGGREGATE", "COINALYZE_JOB", "CollectResult", "coinalyze_job", "collect"]
