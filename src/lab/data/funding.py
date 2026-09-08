"""Хранилище ставок фандинга: `root/funding/venue=…/instrument=…/YYYY-MM.parquet`.

Зачем отдельно от свечей: у фандинга своя сетка времени (раз в 8 часов у большинства
площадок, раз в час у Hyperliquid) и одно число вместо OHLCV. Класть его в свечи значило бы
подстраивать одно под другое.

Зачем вообще: у стратегий, не зависящих от направления рынка (фандинг-арбитраж, базис,
нейтральные сетки), ВЕСЬ доход — в этой ставке. Пока в симуляторе стояла константа из
параметра, такие стратегии мерить было бессмысленно: результат определялся выдуманным
числом, а не рынком.

Запись идемпотентна: партиция сливается с существующей, дубли по времени отбрасываются.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA = pa.schema(
    [
        ("ts", pa.timestamp("us", tz="UTC")),
        ("rate", pa.decimal128(20, 12)),
    ]
)
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe(part: str) -> str:
    return _SAFE.sub("_", part)


def _month(ts: datetime) -> str:
    return f"{ts:%Y-%m}"


@dataclass(frozen=True)
class FundingRate:
    """Одна выплата: время расчёта и ставка за период (доля, не проценты)."""

    ts: datetime
    rate: Decimal


class FundingStore:
    """Ставки фандинга по площадкам и инструментам."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path(self, venue: str, instrument: str) -> Path:
        return self.root / "funding" / f"venue={_safe(venue)}" / f"instrument={_safe(instrument)}"

    def partition_files(self, venue: str, instrument: str) -> list[Path]:
        directory = self.path(venue, instrument)
        return sorted(directory.glob("*.parquet")) if directory.exists() else []

    def write(self, venue: str, instrument: str, rates: Iterable[FundingRate]) -> int:
        """Записать ставки; возвращает, сколько строк добавилось (дубли не считаются)."""
        rows = sorted(rates, key=lambda r: r.ts)
        if not rows:
            return 0
        directory = self.path(venue, instrument)
        directory.mkdir(parents=True, exist_ok=True)
        written = 0
        for month in sorted({_month(r.ts) for r in rows}):
            part = [r for r in rows if _month(r.ts) == month]
            table = pa.Table.from_arrays(
                [
                    pa.array([r.ts for r in part], type=SCHEMA.field("ts").type),
                    pa.array([r.rate for r in part], type=SCHEMA.field("rate").type),
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
            # Сравниваем СОДЕРЖИМОЕ, а не число строк: площадка иногда уточняет ставку задним
            # числом, и тогда строк столько же, а значение другое. Проверка по количеству
            # молча оставляла бы старое.
            if existing is not None and merged.equals(existing):
                continue
            tmp = file.with_suffix(".parquet.tmp")
            pq.write_table(merged, tmp)
            tmp.replace(file)
            written += merged.num_rows - before
        return written

    def read(
        self, venue: str, instrument: str, from_ts: datetime, to_ts: datetime
    ) -> list[FundingRate]:
        """Ставки в окне [from_ts, to_ts)."""
        rows = self.query(
            "select ts, rate from {funding} where ts >= ? and ts < ? order by ts",
            venue,
            instrument,
            params=[from_ts, to_ts],
        )
        return [FundingRate(ts=r["ts"], rate=Decimal(str(r["rate"]))) for r in rows]

    def count(self, venue: str, instrument: str) -> int:
        rows = self.query("select count(*) as n from {funding}", venue, instrument)
        return int(rows[0]["n"]) if rows else 0

    def last_ts(self, venue: str, instrument: str) -> datetime | None:
        rows = self.query("select max(ts) as t from {funding}", venue, instrument)
        return rows[0]["t"] if rows and rows[0]["t"] is not None else None

    def query(
        self, sql: str, venue: str, instrument: str, *, params: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        """SQL через DuckDB; `{funding}` в запросе — все партиции инструмента."""
        files = self.partition_files(venue, instrument)
        if not files:
            return []
        glob = str(self.path(venue, instrument) / "*.parquet").replace("'", "''")
        con = duckdb.connect()
        try:
            con.execute("set TimeZone='UTC'")
            src = f"(select ts::TIMESTAMP as ts, rate from read_parquet('{glob}'))"
            cur = con.execute(sql.replace("{funding}", src), params or [])
            names = [d[0] for d in cur.description]
            out = []
            for row in cur.fetchall():
                item = dict(zip(names, row, strict=False))
                if isinstance(item.get("ts"), datetime) and item["ts"].tzinfo is None:
                    item["ts"] = item["ts"].replace(tzinfo=UTC)
                if isinstance(item.get("t"), datetime) and item["t"].tzinfo is None:
                    item["t"] = item["t"].replace(tzinfo=UTC)
                out.append(item)
            return out
        finally:
            con.close()


def _dedupe_sort(table: pa.Table) -> pa.Table:
    """Одна ставка на момент времени, ряд по возрастанию."""
    seen: dict[datetime, Decimal] = {}
    for ts, rate in zip(
        table.column("ts").to_pylist(), table.column("rate").to_pylist(), strict=False
    ):
        seen[ts] = rate
    order = sorted(seen)
    return pa.Table.from_arrays(
        [
            pa.array(order, type=SCHEMA.field("ts").type),
            pa.array([seen[ts] for ts in order], type=SCHEMA.field("rate").type),
        ],
        schema=SCHEMA,
    )


def rates_lookup(rates: Sequence[FundingRate]) -> dict[datetime, Decimal]:
    """Ставки как словарь «время расчёта → ставка» — в таком виде их берёт симулятор."""
    return {r.ts: r.rate for r in rates}


__all__ = ["SCHEMA", "FundingRate", "FundingStore", "rates_lookup"]
