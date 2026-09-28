"""Хранилище метрик ПОЗИЦИОНИРОВАНИЯ: `root/positioning/venue=…/instrument=…/YYYY-MM.parquet`.

Что здесь лежит и зачем. Биржа раз в пять минут публикует, СКОЛЬКО плеча стоит в рынке
(открытый интерес) и КАК оно распределено (лонг/шорт у крупных счетов, поток тейкеров).
Это данные другой природы, чем цена и чем ставка фандинга.

Почему отдельное хранилище, а не колонки к фандингу: проверка 11.09.2026 показала, что
связь ИЗМЕНЕНИЯ открытого интереса со ставкой фандинга равна −0.03, то есть их нет вовсе
(у уровней +0.38). Ставка говорит, сколько ПЛАТЯТ за плечо, открытый интерес — сколько
его НАБРАЛИ. Это разные вопросы, и складывать их в один ряд значило бы потерять второй.

Формат источника (архив Binance, `futures/um/daily/metrics`): посуточные файлы по 289
строк, колонки `create_time, symbol, sum_open_interest, sum_open_interest_value,
count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, count_long_short_ratio,
sum_taker_long_short_vol_ratio`. Помесячных файлов у метрик НЕТ — только посуточные,
поэтому сбор дорогой: 365 запросов на символ в год.

Запись идемпотентна, как у ставок: партиция сливается, дубли по времени отбрасываются,
содержимое сравнивается целиком (биржа уточняет задним числом).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from lab.data.paths import safe_part as _safe

_NUM = pa.decimal128(30, 12)
SCHEMA = pa.schema(
    [
        ("ts", pa.timestamp("us", tz="UTC")),
        ("open_interest", _NUM),  # в базовой монете
        ("open_interest_value", _NUM),  # он же в долларах
        ("top_accounts_ratio", _NUM),  # лонг/шорт по ЧИСЛУ крупных счетов
        ("top_positions_ratio", _NUM),  # лонг/шорт по ОБЪЁМУ их позиций
        ("accounts_ratio", _NUM),  # лонг/шорт по всем счетам
        ("taker_ratio", _NUM),  # агрессивные покупки к продажам
    ]
)
_FIELDS = tuple(f.name for f in SCHEMA if f.name != "ts")


def _month(ts: datetime) -> str:
    return f"{ts:%Y-%m}"


@dataclass(frozen=True)
class Positioning:
    """Один пятиминутный снимок позиционирования."""

    ts: datetime
    open_interest: Decimal
    open_interest_value: Decimal
    top_accounts_ratio: Decimal
    top_positions_ratio: Decimal
    accounts_ratio: Decimal
    taker_ratio: Decimal


class PositioningStore:
    """Метрики позиционирования по площадкам и инструментам."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path(self, venue: str, instrument: str) -> Path:
        return (
            self.root / "positioning" / f"venue={_safe(venue)}" / f"instrument={_safe(instrument)}"
        )

    def partition_files(self, venue: str, instrument: str) -> list[Path]:
        directory = self.path(venue, instrument)
        return sorted(directory.glob("*.parquet")) if directory.exists() else []

    def write(self, venue: str, instrument: str, rows: Iterable[Positioning]) -> int:
        ordered = sorted(rows, key=lambda r: r.ts)
        if not ordered:
            return 0
        directory = self.path(venue, instrument)
        directory.mkdir(parents=True, exist_ok=True)
        written = 0
        for month in sorted({_month(r.ts) for r in ordered}):
            part = [r for r in ordered if _month(r.ts) == month]
            table = pa.Table.from_arrays(
                [pa.array([r.ts for r in part], type=SCHEMA.field("ts").type)]
                + [
                    pa.array([getattr(r, name) for r in part], type=_NUM)
                    for name in _FIELDS
                ],
                schema=SCHEMA,
            )
            file = directory / f"{month}.parquet"
            before = 0
            existing: pa.Table | None = None
            if file.exists():
                existing = pq.read_table(file, schema=SCHEMA)
                before = existing.num_rows
                table = pa.concat_tables([existing, table])
            merged = _dedupe_sort(table)
            # Сравниваем содержимое, а не число строк: биржа уточняет метрики задним числом.
            if existing is not None and merged.equals(existing):
                continue
            tmp = file.with_suffix(".parquet.tmp")
            pq.write_table(merged, tmp)
            tmp.replace(file)
            written += merged.num_rows - before
        return written

    def read(
        self, venue: str, instrument: str, from_ts: datetime, to_ts: datetime
    ) -> list[Positioning]:
        rows = self.query(
            "select * from {positioning} where ts >= ? and ts < ? order by ts",
            venue,
            instrument,
            params=[from_ts, to_ts],
        )
        return [
            Positioning(
                ts=r["ts"], **{name: Decimal(str(r[name])) for name in _FIELDS}
            )
            for r in rows
        ]

    def daily(
        self, venue: str, instrument: str, from_ts: datetime, to_ts: datetime
    ) -> list[dict[str, Any]]:
        """Суточные средние — СРАЗУ в запросе, а не чтением всех снимков в память.

        Пятиминутный шаг даёт 288 строк в сутки: четыре года это 431 864 снимка,
        и материализовать их объектами значит занять под гигабайт. Первый же замер
        так и получил SIGKILL от cgroup — молча, без единой строки вывода. Усреднение
        принадлежит движку запроса, а правилу нужны только суточные числа.
        """
        fields = ", ".join(f"avg({name}) as {name}" for name in _FIELDS)
        return self.query(
            f"select date_trunc('day', ts) as ts, {fields} from {{positioning}} "
            "where ts >= ? and ts < ? group by 1 order by 1",
            venue,
            instrument,
            params=[from_ts, to_ts],
        )

    def count(self, venue: str, instrument: str) -> int:
        rows = self.query("select count(*) as n from {positioning}", venue, instrument)
        return int(rows[0]["n"]) if rows else 0

    def last_ts(self, venue: str, instrument: str) -> datetime | None:
        rows = self.query("select max(ts) as t from {positioning}", venue, instrument)
        return rows[0]["t"] if rows and rows[0]["t"] is not None else None

    def query(
        self, sql: str, venue: str, instrument: str, *, params: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        files = self.partition_files(venue, instrument)
        if not files:
            return []
        glob = str(self.path(venue, instrument) / "*.parquet").replace("'", "''")
        con = duckdb.connect()
        try:
            from lab.data.store import _tame

            _tame(con)
            con.execute("set TimeZone='UTC'")
            src = f"(select * from read_parquet('{glob}'))"
            cur = con.execute(sql.replace("{positioning}", src), params or [])
            names = [d[0] for d in cur.description]
            return [
                {
                    k: (
                        v.replace(tzinfo=UTC)
                        if isinstance(v, datetime) and v.tzinfo is None
                        else v
                    )
                    for k, v in zip(names, row, strict=True)
                }
                for row in cur.fetchall()
            ]
        finally:
            con.close()


def _dedupe_sort(table: pa.Table) -> pa.Table:
    rows: dict[datetime, dict[str, Any]] = {}
    for r in table.to_pylist():
        rows[r["ts"]] = r  # последняя запись по времени побеждает
    return pa.Table.from_pylist([rows[k] for k in sorted(rows)], schema=SCHEMA)


def daily_mean(rows: Iterable[Positioning], field: str) -> dict[Any, Decimal]:
    """Средние по суткам: пятиминутный шаг для правил слишком шумен.

    Считать сигнал на 5 минутах — значит торговать шум: у открытого интереса внутри дня
    ходят проценты, а информативно движение за дни.
    """
    buckets: dict[Any, list[Decimal]] = {}
    for row in rows:
        buckets.setdefault(row.ts.date(), []).append(getattr(row, field))
    return {day: sum(vals, Decimal(0)) / len(vals) for day, vals in buckets.items() if vals}


__all__ = ["SCHEMA", "Positioning", "PositioningStore", "daily_mean"]
