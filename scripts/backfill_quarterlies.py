#!/usr/bin/env python
"""Свечи КВАРТАЛЬНЫХ фьючерсов Binance (USDⓈ-M) для базисных стратегий.

Отдельно от `lab data backfill` по двум причинам, и обе — про то, что срочный контракт
живёт квартал, а не вечно.

1. Истёкшего контракта нет в справочнике биржи: `ccxt` на `BTCUSDT_220325` отвечает
   «does not have market symbol», поэтому единственный источник — архив, без отката на REST.
2. Окно качания у каждого контракта своё. Общее пятилетнее окно означало бы полсотни
   запросов за месяцы, когда контракта не существовало. Границы берутся из каталога
   архива: какие месяцы есть, те и качаем.

    python scripts/backfill_quarterlies.py --dry-run
    python scripts/backfill_quarterlies.py --bases BTC,ETH --tf 1h
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.contracts import Candle  # noqa: E402
from lab.data import CandleStore  # noqa: E402
from lab.data.backfill import backfill  # noqa: E402
from lab.data.backfill_cex import BinanceArchive  # noqa: E402

LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
PREFIX = "data/futures/um/monthly/klines/"


def contracts(client: httpx.Client, bases: Sequence[str]) -> list[str]:
    """Символы срочных контрактов в архиве: `BTCUSDT_260925` (у бессрочного даты нет)."""
    out: list[str] = []
    marker: str | None = None
    wanted = tuple(f"{b}USDT_" for b in bases)
    for _ in range(30):
        params = {"delimiter": "/", "prefix": PREFIX}
        if marker:
            params["marker"] = marker
        r = client.get(LISTING, params=params)
        r.raise_for_status()
        found = re.findall(rf"<Prefix>{PREFIX}([^/]+)/</Prefix>", r.text)
        if not found:
            break
        out += [s for s in found if s.startswith(wanted) and s.split("_")[-1].isdigit()]
        if "<IsTruncated>true</IsTruncated>" not in r.text:
            break
        marker = PREFIX + found[-1] + "/"
    return sorted(set(out))


def months_of(client: httpx.Client, symbol: str, tf: str) -> list[datetime]:
    """Какие месяцы контракта лежат в архиве — это и есть срок его жизни."""
    prefix = f"{PREFIX}{symbol}/{tf}/"
    r = client.get(LISTING, params={"prefix": prefix})
    r.raise_for_status()
    stamps = re.findall(rf"{re.escape(symbol)}-{re.escape(tf)}-(\d{{4}})-(\d{{2}})\.zip", r.text)
    return sorted({datetime(int(y), int(m), 1, tzinfo=UTC) for y, m in stamps})


def ccxt_name(symbol: str) -> str:
    """`BTCUSDT_260925` → `BTC/USDT:USDT-260925` — так контракт зовут фид и стратегия."""
    head, _, expiry = symbol.partition("_")
    base = head[: -len("USDT")]
    return f"{base}/USDT:USDT-{expiry}"


def archive_only(archive: BinanceArchive):
    """Источник ТОЛЬКО из архива: отката на REST быть не может, контракт уже не торгуется.

    Месяц без файла — это не сбой, а месяцы до листинга и после расчёта: контракта тогда
    просто не было. Поэтому пустой ответ здесь нормален, в отличие от живого инструмента.
    """

    def fetch(instrument: str, tf: str, from_ts: datetime, to_ts: datetime) -> list[Candle]:
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
    ap.add_argument("--bases", default="BTC,ETH")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--root", default="data")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    bases = [b.strip().upper() for b in args.bases.split(",") if b.strip()]

    with httpx.Client(timeout=60) as client:
        symbols = contracts(client, bases)
        print(f"срочных контрактов в архиве: {len(symbols)}", flush=True)
        plan: list[tuple[str, datetime, datetime]] = []
        for symbol in symbols:
            months = months_of(client, symbol, args.tf)
            if not months:
                print(f"  {symbol}: нет месяцев для {args.tf}", flush=True)
                continue
            last = (months[-1] + timedelta(days=32)).replace(day=1)
            plan.append((symbol, months[0], min(last, datetime.now(UTC))))

    for symbol, a, b in plan:
        print(f"  {ccxt_name(symbol)}: {a:%Y-%m} … {b:%Y-%m}", flush=True)
    if args.dry_run:
        return 0

    store = CandleStore(args.root)
    source = archive_only(BinanceArchive())
    written = failed = 0
    for symbol, a, b in plan:
        name = ccxt_name(symbol)
        try:
            res = backfill(store, source, "binance", name, args.tf, a, b, chunk=timedelta(days=31))
        except Exception as err:  # noqa: BLE001 — один контракт не должен ронять остальные
            failed += 1
            print(f"  ошибка {name}: {err}", flush=True)
            continue
        written += res.rows_written
        print(f"  {name}: {res.rows_written} свечей", flush=True)
    print(f"готово: контрактов {len(plan)}, свечей {written}, ошибок {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
