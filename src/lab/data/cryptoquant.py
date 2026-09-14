"""Суточные ряды CryptoQuant: `root/cryptoquant/asset=…/exchange=…/YYYY-MM.parquet`.

**Зачем отдельное хранилище, а не разовые запросы.** Бесплатный тариф CryptoQuant отдаёт
скользящее окно в ТРИДЦАТЬ последних суток и ничего больше: запрос с `from=20240101`
возвращает `400 Out of allowed request range`, а не укороченный ответ. Проверено 14.09.2026
живым вызовом по всем нужным точкам. Значит на истории этим источником нельзя проверить
ни одной гипотезы — данные существуют, только если мы сохраняем их каждый день сами,
и каждые несобранные сутки теряются навсегда.

**Что бесплатный тариф даёт, а что нет.** Раздел `market-data` открыт целиком на суточном
окне; `exchange-flows` (резервы бирж), `flow-indicator`, `network-*` и `market-indicator` —
Professional и выше, часовое окно — Advanced. То есть главного, ради чего ключ заводился
(резервы бирж), у нас нет; зато есть **ликвидации по площадкам**, которых нет в архивах
Binance вовсе, и **премия Coinbase**, которой нет нигде.

Ликвидации и премия Coinbase — не дубликат `PositioningStore`. Тот собран из архива Binance
(одна площадка, пятиминутный шаг, история с 2020) и отвечает, СКОЛЬКО плеча набрано;
здесь — сколько его вынесло, и на каких площадках.

Запись идемпотентна, как у ставок и позиционирования: партиция сливается, дубли по времени
отбрасываются, содержимое сравнивается целиком (источник уточняет сутки задним числом,
и сравнение по ЧИСЛУ строк молча оставляло бы старое значение).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

_NUM = pa.decimal128(30, 12)

# Псевдоплощадка для рядов, у которых измерения «биржа» нет вовсе (премия Coinbase
# считается по рынку). Держать их в том же хранилище правильнее, чем заводить второе:
# шаг, происхождение и срок жизни у них общие.
MARKET = "market"

SCHEMA = pa.schema(
    [
        ("ts", pa.timestamp("us", tz="UTC")),
        # ликвидации: в монете и в долларах, отдельно вынос лонгов и шортов
        ("long_liq", _NUM),
        ("short_liq", _NUM),
        ("long_liq_usd", _NUM),
        ("short_liq_usd", _NUM),
        # поток тейкеров: агрессивные покупки против агрессивных продаж
        ("taker_buy", _NUM),
        ("taker_sell", _NUM),
        ("taker_buy_ratio", _NUM),
        # прочее из market-data
        ("open_interest", _NUM),
        ("funding_rate", _NUM),
        ("coinbase_premium_gap", _NUM),
        ("coinbase_premium_index", _NUM),
    ]
)
FIELDS = tuple(f.name for f in SCHEMA if f.name != "ts")
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


_SCALE = Decimal(1).scaleb(-12)  # 1e-12 — ровно масштаб колонки


def _fit(value: Decimal | None) -> Decimal | None:
    """Привести число к масштабу колонки ЯВНО, а не надеяться на pyarrow.

    Источник отдаёт отношения с полной машинной точностью (0.9999999999999999),
    а колонка держит 12 знаков после запятой. pyarrow в таком случае не округляет
    молча, а отказывается целиком: `ArrowInvalid: Rescaling Decimal value would cause
    data loss` — и падает ВЕСЬ суточный проход из-за одного числа. Мы теряем
    тринадцатый знак сознательно: для ставок и объёмов он не значит ничего.
    """
    if value is None:
        return None
    try:
        return value.quantize(_SCALE, rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        # Число не влезает в 30 разрядов целиком — такого у рыночных данных быть
        # не должно, и записать его молча обрезанным хуже, чем не записать вовсе.
        return None


def _safe(part: str) -> str:
    return _SAFE.sub("_", part)


def _month(ts: datetime) -> str:
    return f"{ts:%Y-%m}"


@dataclass(frozen=True)
class DailyRow:
    """Одни сутки одного ряда. Ненаблюдённое поле — `None`, а не ноль.

    Разница не косметическая: ноль ликвидаций — это утверждение «в этот день никого
    не вынесло», а `None` — «мы не спрашивали». Бесплатный тариф даёт разные наборы полей
    по разным площадкам, и подмена второго первым создала бы ряды, которых не было.
    """

    ts: datetime
    long_liq: Decimal | None = None
    short_liq: Decimal | None = None
    long_liq_usd: Decimal | None = None
    short_liq_usd: Decimal | None = None
    taker_buy: Decimal | None = None
    taker_sell: Decimal | None = None
    taker_buy_ratio: Decimal | None = None
    open_interest: Decimal | None = None
    funding_rate: Decimal | None = None
    coinbase_premium_gap: Decimal | None = None
    coinbase_premium_index: Decimal | None = None

    def merge(self, other: DailyRow) -> DailyRow:
        """Склеить две части одних суток (разные точки API приходят разными запросами)."""
        values = {}
        for name in FIELDS:
            fresh = getattr(other, name)
            values[name] = fresh if fresh is not None else getattr(self, name)
        return DailyRow(ts=self.ts, **values)


class CryptoQuantStore:
    """Суточные ряды по монете и площадке."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path(self, asset: str, exchange: str) -> Path:
        return (
            self.root / "cryptoquant" / f"asset={_safe(asset)}" / f"exchange={_safe(exchange)}"
        )

    def partition_files(self, asset: str, exchange: str) -> list[Path]:
        directory = self.path(asset, exchange)
        return sorted(directory.glob("*.parquet")) if directory.exists() else []

    def write(self, asset: str, exchange: str, rows: Iterable[DailyRow]) -> int:
        ordered = sorted(rows, key=lambda r: r.ts)
        if not ordered:
            return 0
        directory = self.path(asset, exchange)
        directory.mkdir(parents=True, exist_ok=True)
        written = 0
        for month in sorted({_month(r.ts) for r in ordered}):
            part = [r for r in ordered if _month(r.ts) == month]
            table = pa.Table.from_arrays(
                [pa.array([r.ts for r in part], type=SCHEMA.field("ts").type)]
                + [
                    pa.array([_fit(getattr(r, name)) for r in part], type=_NUM)
                    for name in FIELDS
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
            if existing is not None and merged.equals(existing):
                continue
            tmp = file.with_suffix(".parquet.tmp")
            pq.write_table(merged, tmp)
            tmp.replace(file)
            written += merged.num_rows - before
        return written

    def read(self, asset: str, exchange: str, from_ts: datetime, to_ts: datetime) -> list[DailyRow]:
        rows = self.query(
            "select * from {cq} where ts >= ? and ts < ? order by ts",
            asset,
            exchange,
            params=[from_ts, to_ts],
        )
        return [
            DailyRow(
                ts=r["ts"],
                **{
                    name: (None if r[name] is None else Decimal(str(r[name])))
                    for name in FIELDS
                },
            )
            for r in rows
        ]

    def count(self, asset: str, exchange: str) -> int:
        rows = self.query("select count(*) as n from {cq}", asset, exchange)
        return int(rows[0]["n"]) if rows else 0

    def last_ts(self, asset: str, exchange: str) -> datetime | None:
        rows = self.query("select max(ts) as t from {cq}", asset, exchange)
        return rows[0]["t"] if rows and rows[0]["t"] is not None else None

    def series(self) -> list[tuple[str, str]]:
        """Какие пары «монета × площадка» уже собираются — для отчёта о накоплении."""
        base = self.root / "cryptoquant"
        if not base.exists():
            return []
        out: list[tuple[str, str]] = []
        for asset_dir in sorted(base.glob("asset=*")):
            for exchange_dir in sorted(asset_dir.glob("exchange=*")):
                if any(exchange_dir.glob("*.parquet")):
                    out.append(
                        (asset_dir.name.split("=", 1)[1], exchange_dir.name.split("=", 1)[1])
                    )
        return out

    def query(
        self, sql: str, asset: str, exchange: str, *, params: list[Any] | None = None
    ) -> list[dict[str, Any]]:
        files = self.partition_files(asset, exchange)
        if not files:
            return []
        glob = str(self.path(asset, exchange) / "*.parquet").replace("'", "''")
        con = duckdb.connect()
        try:
            from lab.data.store import _tame

            _tame(con)
            con.execute("set TimeZone='UTC'")
            src = f"(select * from read_parquet('{glob}'))"
            cur = con.execute(sql.replace("{cq}", src), params or [])
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
    """Дубли по времени сливаются ПОЛЯМИ, а не заменой строки целиком.

    Иначе второй запрос суток (например, только премия Coinbase) затёр бы ликвидации,
    записанные первым, пустыми значениями — и ряд молча обеднел бы.
    """
    rows: dict[datetime, dict[str, Any]] = {}
    for r in table.to_pylist():
        prev = rows.get(r["ts"])
        if prev is None:
            rows[r["ts"]] = r
            continue
        rows[r["ts"]] = {
            "ts": r["ts"],
            **{
                name: (r[name] if r[name] is not None else prev[name])
                for name in FIELDS
            },
        }
    return pa.Table.from_pylist([rows[k] for k in sorted(rows)], schema=SCHEMA)


__all__ = ["FIELDS", "MARKET", "SCHEMA", "CryptoQuantStore", "DailyRow"]
