#!/usr/bin/env python
"""Свечи ТОЛЬКО из архива — для инструментов, которых больше нет на бирже.

`lab data backfill` ходит в ccxt, и делистнутый контракт получает «binance does not have
market symbol». Именно эти инструменты и нужны для честной вселенной: AKRO, BTS, BTT,
BZRX — монеты, которые торговались, попадали в отборы и умерли. Без них любой замер
меряет только выживших и показывает прибыль всегда.

Границы качания берутся из каталога архива: какие месяцы у символа есть, тот срок он
и прожил. Обрыв на одном символе не роняет остальные.

    python scripts/backfill_archive.py --file /app/data/dead.txt --tf 1d
    python scripts/backfill_archive.py --symbols BTT/USDT:USDT,BTS/USDT:USDT --tf 1d
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.contracts import Candle  # noqa: E402
from lab.data import CandleStore  # noqa: E402
from lab.data.backfill import backfill  # noqa: E402
from lab.data.backfill_cex import BinanceArchive  # noqa: E402

LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"


def archive_symbol(instrument: str) -> tuple[str, str]:
    """`BTT/USDT:USDT` → (`futures/um`, `BTTUSDT`); `BTT/USDT` → (`spot`, `BTTUSDT`)."""
    base, rest = instrument.split("/")
    quote = rest.partition(":")[0]
    kind = "futures/um" if ":" in instrument else "spot"
    return kind, f"{base}{quote}".upper()


def months_of(client: httpx.Client, instrument: str, tf: str) -> list[datetime]:
    """Какие месяцы лежат в архиве — это и есть срок жизни инструмента."""
    kind, symbol = archive_symbol(instrument)
    prefix = f"data/{kind}/monthly/klines/{symbol}/{tf}/"
    r = client.get(LISTING, params={"prefix": prefix})
    r.raise_for_status()
    stamps = re.findall(rf"{re.escape(symbol)}-{re.escape(tf)}-(\d{{4}})-(\d{{2}})\.zip", r.text)
    return sorted({datetime(int(y), int(m), 1, tzinfo=UTC) for y, m in stamps})


def archive_only(archive: BinanceArchive, tf: str):
    """Источник без отката на REST: инструмента на бирже уже нет, спрашивать некого.

    Месяц без файла — не сбой, а время до листинга или после делистинга.
    """

    def fetch(instrument: str, _tf: str, from_ts: datetime, to_ts: datetime) -> list[Candle]:
        out: list[Candle] = []
        cursor = from_ts
        while cursor < to_ts:
            month = cursor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            nxt = (month + timedelta(days=32)).replace(day=1)
            upto = min(nxt, to_ts)
            rows = archive.month(instrument, tf, month) or []
            out.extend(c for c in rows if cursor <= c.ts < upto)
            cursor = upto
        return out

    return fetch


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", help="через запятую, в именах ccxt")
    ap.add_argument("--file", help="файл со списком (по одному в строке)")
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    names: list[str] = []
    if args.file:
        names += [
            line.strip()
            for line in Path(args.file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if args.symbols:
        names += [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not names:
        print("нужен --file или --symbols", file=sys.stderr)
        return 2

    store = CandleStore(args.root)
    archive = BinanceArchive()
    source = archive_only(archive, args.tf)
    written = failed = empty = 0
    with httpx.Client(timeout=60) as client:
        for name in names:
            try:
                months = months_of(client, name, args.tf)
            except httpx.HTTPError as err:
                failed += 1
                print(f"  каталог {name}: {type(err).__name__}", flush=True)
                continue
            if not months:
                empty += 1
                print(f"  {name}: в архиве нет месяцев для {args.tf}", flush=True)
                continue
            a = months[0]
            b = min((months[-1] + timedelta(days=32)).replace(day=1), datetime.now(UTC))
            try:
                res = backfill(
                    store, source, "binance", name, args.tf, a, b, chunk=timedelta(days=31)
                )
            except Exception as err:  # noqa: BLE001 — один символ не роняет остальные
                failed += 1
                print(f"  {name}: {type(err).__name__}: {err}", flush=True)
                continue
            written += res.rows_written
            print(f"  {name}: {res.rows_written} свечей ({a:%m.%Y}…{months[-1]:%m.%Y})", flush=True)
    print(f"готово: символов {len(names)}, свечей {written}, пусто {empty}, ошибок {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
