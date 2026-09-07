"""История флора и объёма коллекций в Parquet (История 72).

Раскладка: `root/nft/collection=<коллекция>/YYYY-MM.parquet` — как у свечей (решение §2).
Запись идемпотентна: ключ снимка — (площадка, момент), повторный прогон того же окна
не удваивает историю, иначе замер «объём вырос» получается из повторного опроса.
Деньги хранятся строками: Decimal в float не переводим (правило проекта).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from lab.feeds.nft.types import CollectionStats

COLUMNS = (
    "collection",
    "chain",
    "market",
    "name",
    "floor",
    "volume_24h",
    "listed",
    "holders",
    "supply",
    "sales_24h",
    "currency",
    "creator",
    "ts",
)
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass
class WriteResult:
    rows_written: int = 0
    rows_skipped: int = 0
    files: list[str] = field(default_factory=list)


def _slug(collection: str) -> str:
    return _SAFE.sub("_", collection)[:120] or "unknown"


def _text(value) -> str:
    return "" if value is None else str(value)


def _row(stats: CollectionStats) -> dict:
    ts = stats.ts if stats.ts.tzinfo else stats.ts.replace(tzinfo=UTC)
    return {
        "collection": stats.collection,
        "chain": stats.chain,
        "market": stats.market,
        "name": stats.name,
        "floor": _text(stats.floor),
        "volume_24h": _text(stats.volume_24h),
        "listed": -1 if stats.listed is None else int(stats.listed),
        "holders": -1 if stats.holders is None else int(stats.holders),
        "supply": -1 if stats.supply is None else int(stats.supply),
        "sales_24h": -1 if stats.sales_24h is None else int(stats.sales_24h),
        "currency": stats.currency,
        "creator": stats.creator,
        "ts": ts.replace(tzinfo=None),
    }


def _stats(row: dict) -> CollectionStats:
    def num(name: str) -> Decimal | None:
        value = row.get(name)
        return Decimal(value) if value else None

    def count(name: str) -> int | None:
        value = row.get(name)
        return None if value is None or int(value) < 0 else int(value)

    ts = row["ts"]
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return CollectionStats(
        collection=row["collection"],
        chain=row.get("chain", ""),
        market=row.get("market", ""),
        name=row.get("name", ""),
        floor=num("floor"),
        volume_24h=num("volume_24h"),
        listed=count("listed"),
        holders=count("holders"),
        supply=count("supply"),
        sales_24h=count("sales_24h"),
        currency=row.get("currency", "USD"),
        creator=row.get("creator", ""),
        ts=ts if ts.tzinfo else ts.replace(tzinfo=UTC),
    )


class CollectionStore:
    """Parquet-история коллекций. Без pyarrow не создаётся — зависимость уже в дереве."""

    def __init__(self, root: str | Path = "data") -> None:
        self.root = Path(root) / "nft"

    def _dir(self, collection: str) -> Path:
        return self.root / f"collection={_slug(collection)}"

    def _file(self, collection: str, ts: datetime) -> Path:
        return self._dir(collection) / f"{ts.year:04d}-{ts.month:02d}.parquet"

    # -- запись ---------------------------------------------------------------------

    def write(self, snapshots: Iterable[CollectionStats]) -> WriteResult:
        import pyarrow as pa
        import pyarrow.parquet as pq

        result = WriteResult()
        by_file: dict[Path, list[dict]] = {}
        for stats in snapshots:
            row = _row(stats)
            by_file.setdefault(self._file(stats.collection, row["ts"]), []).append(row)

        for path, rows in by_file.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            existing: list[dict] = []
            if path.exists():
                existing = pq.read_table(path).to_pylist()
            seen = {(r["market"], r["ts"]) for r in existing}
            fresh: list[dict] = []
            for row in rows:
                key = (row["market"], row["ts"])
                if key in seen:
                    result.rows_skipped += 1
                    continue
                seen.add(key)
                fresh.append(row)
            if not fresh:
                continue
            merged = sorted(existing + fresh, key=lambda r: (r["ts"], r["market"]))
            table = pa.Table.from_pylist(merged, schema=self._schema())
            pq.write_table(table, path)
            result.rows_written += len(fresh)
            result.files.append(str(path))
        return result

    @staticmethod
    def _schema():
        import pyarrow as pa

        return pa.schema(
            [
                ("collection", pa.string()),
                ("chain", pa.string()),
                ("market", pa.string()),
                ("name", pa.string()),
                ("floor", pa.string()),
                ("volume_24h", pa.string()),
                ("listed", pa.int64()),
                ("holders", pa.int64()),
                ("supply", pa.int64()),
                ("sales_24h", pa.int64()),
                ("currency", pa.string()),
                ("creator", pa.string()),
                ("ts", pa.timestamp("us")),
            ]
        )

    # -- чтение ---------------------------------------------------------------------

    def read(
        self,
        collection: str,
        *,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        market: str | None = None,
    ) -> list[CollectionStats]:
        import pyarrow.parquet as pq

        folder = self._dir(collection)
        if not folder.exists():
            return []
        rows: list[dict] = []
        for path in sorted(folder.glob("*.parquet")):
            rows.extend(pq.read_table(path).to_pylist())
        out = [_stats(r) for r in rows]
        if market:
            out = [s for s in out if s.market == market]
        if from_ts:
            out = [s for s in out if s.ts >= from_ts]
        if to_ts:
            out = [s for s in out if s.ts <= to_ts]
        return sorted(out, key=lambda s: s.ts)

    def last(self, collection: str, *, market: str | None = None) -> CollectionStats | None:
        rows = self.read(collection, market=market)
        return rows[-1] if rows else None

    def count(self, collection: str) -> int:
        return len(self.read(collection))

    def collections(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            p.name.split("=", 1)[1] for p in self.root.iterdir() if p.name.startswith("collection=")
        )

    def floor_series(self, collection: str, **kw) -> list[tuple[datetime, Decimal]]:
        return [(s.ts, s.floor) for s in self.read(collection, **kw) if s.floor is not None]

    def query(self, sql: str, *, collection: str | None = None):
        """Произвольный запрос по истории через DuckDB (как в `data.CandleStore`)."""
        import duckdb

        folder = self._dir(collection) if collection else self.root
        pattern = str(folder / ("*.parquet" if collection else "*/*.parquet"))
        con = duckdb.connect()
        try:
            con.execute(f"CREATE VIEW nft AS SELECT * FROM read_parquet('{pattern}')")
            return con.execute(sql).fetchall()
        finally:
            con.close()


def sales_per_minute(sales: Sequence, *, window_min: int, now: datetime | None = None) -> Decimal:
    """Скорость продаж за окно — правило входа ранней вторички (История 75)."""
    if window_min <= 0:
        return Decimal(0)
    at = now or datetime.now(UTC)
    edge = at.timestamp() - window_min * 60
    # окно полуоткрытое: продажа ровно на границе принадлежит предыдущему окну,
    # иначе соседние окна считают её дважды
    hits = sum(1 for s in sales if s.ts.timestamp() > edge)
    return Decimal(hits) / Decimal(window_min)
