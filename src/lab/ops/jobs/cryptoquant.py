"""Ежедневный сбор суточных рядов CryptoQuant — единственный способ получить историю.

Бесплатный тариф отдаёт скользящее окно в 30 суток и отказывает на любых датах старше
(`400 Out of allowed request range`). Поэтому здесь не «обновление кэша», а **накопление**:
не собранные сегодня сутки нельзя будет собрать никогда.

Из этого следуют два решения, которые иначе выглядели бы избыточными:

* **окно берётся целиком, все 30 суток, а не «со вчера»**. Стоит это столько же — один
  запрос, — а перекрытие закрывает дыру после любого простоя: упал сервер на неделю,
  следующий же запуск восстановит всё. Запись идемпотентна, повтор ничего не портит.
* **отказ одной точки не отменяет проход**. Тариф отдаёт разные наборы по разным монетам
  (`sol` закрыт, `xrp` открыт), и падение на первой дыре означало бы, что не собрано ничего.
  Пропуски попадают в отчёт и в предупреждения лога, а не гасят задание.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lab.data.cryptoquant import CryptoQuantStore
from lab.feeds.cryptoquant import (
    CryptoQuantConfig,
    CryptoQuantFeed,
    load_cryptoquant,
    make_feed,
    plan_series,
)

log = logging.getLogger(__name__)

CRYPTOQUANT_JOB = "cryptoquant"


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
            return f"CryptoQuant: не собрано ({self.error})"
        tail = f", пропусков {len(self.skipped)}" if self.skipped else ""
        return (
            f"CryptoQuant: рядов {self.series}, суток {self.rows}, "
            f"новых строк {self.written}{tail}"
        )


def data_root() -> Path:
    import os

    return Path(os.environ.get("LAB_DATA_ROOT") or "data")


def collect(
    *,
    feed: CryptoQuantFeed | None = None,
    store: CryptoQuantStore | None = None,
    config: CryptoQuantConfig | None = None,
    now: datetime | None = None,
) -> CollectResult:
    """Пройти все пары «монета × площадка» и дописать окно в хранилище."""
    cfg = config or load_cryptoquant()
    src = feed if feed is not None else make_feed(pause_s=cfg.pause_s)
    if not src.enabled:
        return CollectResult(error="CRYPTOQUANT_API_KEY не задан")
    dst = store or CryptoQuantStore(data_root())
    at = now or datetime.now(UTC)
    result = CollectResult()

    for asset, exchange in plan_series(cfg):
        rows, notes = src.collect(asset, exchange)
        result.skipped.extend(notes)
        if not rows:
            continue
        # Сегодняшние сутки ещё не закрыты: источник отдаёт их частично, и записанное
        # сейчас значение завтра окажется меньше настоящего. Перезапись это исправит
        # (партиция сливается), но в ряду до тех пор стояло бы неверное число.
        closed = [r for r in rows if r.ts.date() < at.date()]
        if not closed:
            continue
        result.series += 1
        result.rows += len(closed)
        result.written += dst.write(asset, exchange, closed)

    for note in result.skipped:
        log.info("CryptoQuant пропуск: %s", note)
    return result


def cryptoquant_job(
    *,
    feed: CryptoQuantFeed | None = None,
    store: CryptoQuantStore | None = None,
    config: CryptoQuantConfig | None = None,
    alert: Callable[[str, dict[str, Any]], Any] | None = None,
):
    """Задание `cryptoquant` (см. `config/schedule.yaml`).

    Отсутствие ключа — не авария и не повод слать карточку: источник просто спит, как
    и любой другой без ключа. Карточка уходит только когда ключ есть, а сбор не удался:
    это единственный случай, когда теряются сутки, которых потом не вернуть.
    """
    from lab.ops.scheduler import Job

    def run() -> CollectResult:
        result = collect(feed=feed, store=store, config=config)
        if result.error and "не задан" not in result.error and alert is not None:
            alert(
                "alert",
                {
                    "service": "cryptoquant",
                    "detail": f"Суточные ряды не собраны: {result.error}",
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        log.info("%s", result)
        return result

    return Job(
        id=CRYPTOQUANT_JOB,
        func=run,
        description="Суточные ряды CryptoQuant (окно 30 суток) — накопление вперёд",
    )


__all__ = ["CRYPTOQUANT_JOB", "CollectResult", "collect", "cryptoquant_job", "data_root"]
