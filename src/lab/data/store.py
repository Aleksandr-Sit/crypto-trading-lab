"""Parquet-хранилище свечей (решение §2): `root/candles/venue=…/instrument=…/tf=…/YYYY-MM.parquet`.

write — идемпотентно: партиция сливается с существующей, дубли по ts отбрасываются, ряд сортируется;
read — через DuckDB по глобу партиций, деньги возвращаются Decimal (в Parquet — decimal128(30, 12)).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from lab.contracts import Candle

log = logging.getLogger(__name__)

_MONEY = pa.decimal128(30, 12)
SCHEMA = pa.schema(
    [
        ("ts", pa.timestamp("us", tz="UTC")),
        ("open", _MONEY),
        ("high", _MONEY),
        ("low", _MONEY),
        ("close", _MONEY),
        ("volume", _MONEY),
    ]
)
_SAFE = re.compile(r"[^A-Za-z0-9._-]")

# DuckDB по умолчанию берёт 80% ПАМЯТИ КОНТЕЙНЕРА и все ядра. В контейнере с лимитом
# 1200 МБ это 960 МБ — ровно столько, чтобы не осталось ни рабочему процессу, ни самому
# Python: три замера минутных стратегий подряд получили SIGKILL от cgroup на 946 МБ,
# и каждый завершился МОЛЧА, без единой строки вывода. Хранилище читается кусками,
# и столько памяти движку запроса не нужно; потоки урезаны, потому что ядра на сервере
# общие с четырьмя соседними проектами.
DUCKDB_MEMORY = "256MB"
DUCKDB_THREADS = 2
DUCKDB_MEMORY_ENV = "LAB_DUCKDB_MEMORY"
DUCKDB_THREADS_ENV = "LAB_DUCKDB_THREADS"


def _safe(part: str) -> str:
    return _SAFE.sub("_", part)


def _tame(con: Any) -> None:
    """Ограничить аппетит DuckDB. Настройки нет в этой сборке — не падать из-за неё."""
    memory, threads = CandleStore._duckdb_limits()
    for statement in (f"set memory_limit='{memory}'", f"set threads={threads}"):
        try:
            con.execute(statement)
        except Exception as err:  # noqa: BLE001 — настройка необязательна, запрос важнее
            log.info("DuckDB: %s не применено (%s)", statement, type(err).__name__)


@dataclass(frozen=True)
class WriteResult:
    rows_written: int
    partitions: list[str] = field(default_factory=list)


class CandleStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path(self, venue: str, instrument: str, tf: str) -> Path:
        return (
            self.root
            / "candles"
            / f"venue={_safe(venue)}"
            / f"instrument={_safe(instrument)}"
            / f"tf={_safe(tf)}"
        )

    def partition_files(self, venue: str, instrument: str, tf: str) -> list[Path]:
        d = self.path(venue, instrument, tf)
        return sorted(d.glob("*.parquet")) if d.is_dir() else []

    # -- запись ----------------------------------------------------------------------

    def write(
        self, venue: str, instrument: str, tf: str, candles: Sequence[Candle] | pa.Table
    ) -> WriteResult:
        table = candles if isinstance(candles, pa.Table) else _to_table(candles)
        if table.num_rows == 0:
            return WriteResult(0)
        d = self.path(venue, instrument, tf)
        d.mkdir(parents=True, exist_ok=True)
        written = 0
        touched: list[str] = []
        months = sorted({_month(ts) for ts in table.column("ts").to_pylist()})
        for month in months:
            part = _filter_month(table, month)
            file = d / f"{month}.parquet"
            before = 0
            if file.exists():
                existing = pq.read_table(file, schema=SCHEMA)
                before = existing.num_rows
                part = pa.concat_tables([existing, part])
            merged = _dedupe_sort(part)
            if merged.num_rows == before:
                continue
            tmp = file.with_suffix(".parquet.tmp")
            pq.write_table(merged, tmp)
            tmp.replace(file)
            written += merged.num_rows - before
            touched.append(month)
        return WriteResult(written, touched)

    # -- чтение ----------------------------------------------------------------------

    def read(
        self, venue: str, instrument: str, tf: str, from_ts: datetime, to_ts: datetime
    ) -> list[Candle]:
        rows = self.query(
            "select ts, open, high, low, close, volume from {candles} "
            "where ts >= ? and ts < ? order by ts",
            venue,
            instrument,
            tf,
            params=[
                from_ts.astimezone(UTC).replace(tzinfo=None),
                to_ts.astimezone(UTC).replace(tzinfo=None),
            ],
        )
        return [
            Candle(
                instrument=instrument,
                tf=tf,
                ts=r["ts"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
            )
            for r in rows
        ]

    def count(self, venue: str, instrument: str, tf: str) -> int:
        rows = self.query("select count(*) as n from {candles}", venue, instrument, tf)
        return int(rows[0]["n"]) if rows else 0

    def last_ts(self, venue: str, instrument: str, tf: str) -> datetime | None:
        rows = self.query("select max(ts) as t from {candles}", venue, instrument, tf)
        return rows[0]["t"] if rows else None

    @staticmethod
    def _duckdb_limits() -> tuple[str, int]:
        """Сколько памяти и потоков разрешено DuckDB. Переопределяется окружением."""
        return (
            os.environ.get(DUCKDB_MEMORY_ENV, "").strip() or DUCKDB_MEMORY,
            int(os.environ.get(DUCKDB_THREADS_ENV, "").strip() or DUCKDB_THREADS),
        )

    def query(
        self, sql: str, venue: str, instrument: str, tf: str, *, params: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        """SQL через DuckDB; `{candles}` в запросе — все партиции инструмента."""
        files = self.partition_files(venue, instrument, tf)
        if not files:
            return []
        glob = str(self.path(venue, instrument, tf) / "*.parquet").replace("'", "''")
        con = duckdb.connect()
        try:
            _tame(con)
            con.execute("set TimeZone='UTC'")
            # ts отдаём наивным UTC (TIMESTAMPTZ в duckdb требует pytz), tz добавляем сами
            src = (
                f"(select ts::TIMESTAMP as ts, open, high, low, close, volume "
                f"from read_parquet('{glob}'))"
            )
            cur = con.execute(sql.replace("{candles}", src), params or [])
            names = [d[0] for d in cur.description]
            return [
                {
                    k: (
                        v.replace(tzinfo=UTC) if isinstance(v, datetime) and v.tzinfo is None else v
                    )
                    for k, v in zip(names, row, strict=True)
                }
                for row in cur.fetchall()
            ]
        finally:
            con.close()


def _month(ts: datetime) -> str:
    return ts.strftime("%Y-%m")


def _to_table(candles: Sequence[Candle]) -> pa.Table:
    return pa.table(
        {
            "ts": [c.ts.astimezone(UTC) for c in candles],
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
        },
        schema=SCHEMA,
    )


def _filter_month(table: pa.Table, month: str) -> pa.Table:
    mask = pa.array([_month(ts) == month for ts in table.column("ts").to_pylist()])
    return table.filter(mask)


def _dedupe_sort(table: pa.Table) -> pa.Table:
    rows: dict[datetime, dict[str, Decimal]] = {}
    for r in table.to_pylist():
        rows[r["ts"]] = r  # последняя запись по ts побеждает
    ordered = [rows[k] for k in sorted(rows)]
    return pa.Table.from_pylist(ordered, schema=SCHEMA)


__all__ = ["SCHEMA", "CandleStore", "WriteResult"]
