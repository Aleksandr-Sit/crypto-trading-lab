"""Импорт свечей из CSV в хранилище лаборатории (Parquet).

Зачем: часть истории уже собрана соседними проектами (`/opt/research/data` — Bybit v5,
свечи с ноября 2020). Качать её заново через API — часы и лишняя нагрузка на биржу,
причём глубже, чем отдаёт API, всё равно не получить.

CSV: заголовок `ts,open,high,low,close,volume`, `ts` — миллисекунды epoch UTC.

    python scripts/import_csv_candles.py --venue bybit --instrument BTC/USDT:USDT \
        --tf 1h --csv /import/BTC_1h.csv

Повторный запуск безопасен: CandleStore.write сливает партицию и отбрасывает дубли по ts.
Пишет в LAB_DATA_ROOT (по умолчанию ./data) — то же хранилище, что читает worker.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lab.data.store import SCHEMA, CandleStore  # noqa: E402

COLUMNS = ("ts", "open", "high", "low", "close", "volume")


def _table(ts: list[datetime], cols: dict[str, list[Decimal]]) -> pa.Table:
    arrays = [pa.array(ts, type=SCHEMA.field("ts").type)]
    arrays += [pa.array(cols[name], type=SCHEMA.field(name).type) for name in COLUMNS[1:]]
    return pa.Table.from_arrays(arrays, schema=SCHEMA)


def read_csv(path: Path, chunk_rows: int) -> Iterator[pa.Table]:
    """CSV → таблицы под схему хранилища, кусками. Деньги — Decimal, время — UTC.

    Кусками, а не целиком: минутный файл — больше миллиона строк, а Decimal на строку
    занимает под сотню байт. На сервере с соседними проектами разовое чтение целиком
    съедает память у них, а не только у нас.
    """
    ts: list[datetime] = []
    cols: dict[str, list[Decimal]] = {c: [] for c in COLUMNS[1:]}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"в {path.name} нет колонок: {', '.join(missing)}")
        for row in reader:
            raw = row["ts"].strip()
            if not raw:
                continue
            # Секунды и миллисекунды различаем по порядку величины, а не по длине строки.
            value = int(float(raw))
            seconds = value / 1000 if value > 10_000_000_000 else value
            ts.append(datetime.fromtimestamp(seconds, tz=UTC))
            for name in COLUMNS[1:]:
                cols[name].append(Decimal(row[name]))
            if len(ts) >= chunk_rows:
                yield _table(ts, cols)
                ts = []
                cols = {c: [] for c in COLUMNS[1:]}
    if ts:
        yield _table(ts, cols)


def main() -> int:
    ap = argparse.ArgumentParser(description="Импорт свечей из CSV в хранилище лаборатории")
    ap.add_argument("--csv", required=True, type=Path, help="файл CSV")
    ap.add_argument("--venue", required=True, help="площадка, например bybit")
    ap.add_argument("--instrument", required=True, help="инструмент, например BTC/USDT:USDT")
    ap.add_argument("--tf", required=True, help="таймфрейм, например 1h")
    ap.add_argument("--root", default=os.environ.get("LAB_DATA_ROOT", "data"))
    ap.add_argument(
        "--chunk-rows",
        type=int,
        default=200_000,
        help="сколько строк держать в памяти за раз (по умолчанию 200000)",
    )
    args = ap.parse_args()

    if not args.csv.exists():
        raise SystemExit(f"нет файла {args.csv}")

    store = CandleStore(args.root)
    rows = written = partitions = 0
    first: datetime | None = None
    last: datetime | None = None
    for table in read_csv(args.csv, args.chunk_rows):
        result = store.write(args.venue, args.instrument, args.tf, table)
        rows += table.num_rows
        written += result.rows_written
        partitions += len(result.partitions)
        if first is None:
            first = table.column("ts")[0].as_py()
        last = table.column("ts")[-1].as_py()
    if rows == 0 or first is None or last is None:
        print(f"{args.csv.name}: пусто, нечего писать")
        return 0
    print(
        f"{args.csv.name}: {rows} строк "
        f"({first:%Y-%m-%d} → {last:%Y-%m-%d}), записано новых {written}, "
        f"партиций тронуто {partitions}"
    )
    print(f"  в хранилище теперь: {store.count(args.venue, args.instrument, args.tf)} свечей")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
