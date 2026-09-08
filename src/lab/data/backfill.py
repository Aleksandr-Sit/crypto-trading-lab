"""Бэкфилл свечей кусками с прогрессом и возобновлением (история 22, «собрать данные»).

Состояние — файл `<партиция>/.backfill.json` с последней успешно записанной границей.
Обрыв источника → BackfillInterrupted(resume_from); повторный вызов продолжает с той же точки.
Сетевых вызовов здесь нет: источник передаётся функцией с сигнатурой Feed.candles.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lab.contracts import Candle
from lab.contracts.timeframes import parse_tf
from lab.data.store import CandleStore

log = logging.getLogger(__name__)

Source = Callable[[str, str, datetime, datetime], Sequence[Candle]]
Progress = Callable[[int, int], None]


class BackfillInterrupted(RuntimeError):
    def __init__(self, reason: str, resume_from: datetime, rows_written: int) -> None:
        super().__init__(f"бэкфилл прерван на {resume_from.isoformat()}: {reason}")
        self.reason = reason
        self.resume_from = resume_from
        self.rows_written = rows_written


@dataclass(frozen=True)
class BackfillResult:
    rows_written: int
    chunks: int
    resumed_from: datetime | None
    skipped: bool = False


def _state_file(store: CandleStore, venue: str, instrument: str, tf: str) -> Path:
    return store.path(venue, instrument, tf) / ".backfill.json"


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, done_until: datetime, from_ts: datetime, to_ts: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "from": from_ts.isoformat(),
                "to": to_ts.isoformat(),
                "done_until": done_until.isoformat(),
            }
        ),
        encoding="utf-8",
    )


def _has_holes(
    store: CandleStore,
    venue: str,
    instrument: str,
    tf: str,
    from_ts: datetime,
    to_ts: datetime,
) -> bool:
    """Есть ли пропуски внутри уже записанного ряда.

    Запись «диапазон загружен» сама по себе ничего не гарантирует: источник мог отдать
    неполный кусок, и тогда состояние говорит «готово», а в ряду дырка (так и случилось
    с архивами Binance по SOL и XRP за февраль–март 2022). Считаем в хранилище: сколько
    свечей лежит между первой и последней против того, сколько их должно быть при шаге tf.
    Границы ряда не проверяем — инструмент мог появиться позже начала окна.
    """
    try:
        rows = store.query(
            "select count(*) as n, min(ts) as first_ts, max(ts) as last_ts from {candles} "
            "where ts >= ? and ts < ?",
            venue,
            instrument,
            tf,
            params=[from_ts, to_ts],
        )
    except Exception as err:  # noqa: BLE001 — недоступное хранилище не повод молча пропускать
        log.info("Проверка непрерывности %s %s %s: %s", venue, instrument, tf, err)
        return False
    if not rows or not rows[0].get("n"):
        return False
    row = rows[0]
    step = parse_tf(tf)
    expected = int((row["last_ts"] - row["first_ts"]) / step) + 1
    return int(row["n"]) < expected


def backfill(
    store: CandleStore,
    source: Source,
    venue: str,
    instrument: str,
    tf: str,
    from_ts: datetime,
    to_ts: datetime,
    *,
    chunk: timedelta = timedelta(days=7),
    progress: Progress | None = None,
) -> BackfillResult:
    from_ts, to_ts = from_ts.astimezone(UTC), to_ts.astimezone(UTC)
    state_path = _state_file(store, venue, instrument, tf)
    state = _load_state(state_path)
    cursor = from_ts
    resumed: datetime | None = None
    if state.get("from") == from_ts.isoformat() and state.get("to") == to_ts.isoformat():
        done = datetime.fromisoformat(state["done_until"])
        holes = _has_holes(store, venue, instrument, tf, from_ts, to_ts)
        if done >= to_ts and not holes:
            if progress:
                progress(_units(from_ts, to_ts, tf), _units(from_ts, to_ts, tf))
            return BackfillResult(0, 0, None, skipped=True)
        if holes:
            # Где именно пропуск, состояние не знает — проходим окно заново. Повторная
            # запись безопасна: store.write сливает партицию и отбрасывает дубли.
            log.info("Ряд %s %s %s дырявый, перезагружаю окно целиком", venue, instrument, tf)
        else:
            cursor = resumed = done
    total = _units(from_ts, to_ts, tf)
    written = chunks = 0
    while cursor < to_ts:
        upto = min(cursor + chunk, to_ts)
        try:
            candles = list(source(instrument, tf, cursor, upto))
        except Exception as err:  # noqa: BLE001 — любая ошибка источника = обрыв, состояние сохранено
            _save_state(state_path, cursor, from_ts, to_ts)
            raise BackfillInterrupted(str(err), cursor, written) from err
        written += store.write(venue, instrument, tf, candles).rows_written
        chunks += 1
        cursor = upto
        _save_state(state_path, cursor, from_ts, to_ts)
        if progress:
            progress(_units(from_ts, cursor, tf), total)
    return BackfillResult(written, chunks, resumed)


def _units(a: datetime, b: datetime, tf: str) -> int:
    return max(0, int((b - a) / parse_tf(tf)))


__all__ = ["BackfillInterrupted", "BackfillResult", "backfill"]
