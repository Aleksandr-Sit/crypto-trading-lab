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
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from lab.data.store import SCHEMA, CandleStore  # noqa: E402

COLUMNS = ("ts", "open", "high", "low", "close", "volume")


def read_csv(path: Path) -> pa.Table:
    """CSV → таблица под схему хранилища. Деньги — Decimal, время — UTC."""
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
    arrays = [pa.array(ts, type=SCHEMA.field("ts").type)]
    arrays += [pa.array(cols[name], type=SCHEMA.field(name).type) for name in COLUMNS[1:]]
    return pa.Table.from_arrays(arrays, schema=SCHEMA)


def main() -> int:
    ap = argparse.ArgumentParser(description="Импорт свечей из CSV в хранилище лаборатории")
    ap.add_argument("--csv", required=True, type=Path, help="файл CSV")
    ap.add_argument("--venue", required=True, help="площадка, например bybit")
    ap.add_argument("--instrument", required=True, help="инструмент, например BTC/USDT:USDT")
    ap.add_argument("--tf", required=True, help="таймфрейм, например 1h")
    ap.add_argument("--root", default=os.environ.get("LAB_DATA_ROOT", "data"))
    args = ap.parse_args()

    if not args.csv.exists():
        raise SystemExit(f"нет файла {args.csv}")

    table = read_csv(args.csv)
    if table.num_rows == 0:
        print(f"{args.csv.name}: пусто, нечего писать")
        return 0

    store = CandleStore(args.root)
    result = store.write(args.venue, args.instrument, args.tf, table)
    first = table.column("ts")[0].as_py()
    last = table.column("ts")[-1].as_py()
    print(
        f"{args.csv.name}: {table.num_rows} строк "
        f"({first:%Y-%m-%d} → {last:%Y-%m-%d}), записано новых {result.rows_written}, "
        f"партиций тронуто {len(result.partitions)}"
    )
    print(f"  в хранилище теперь: {store.count(args.venue, args.instrument, args.tf)} свечей")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
