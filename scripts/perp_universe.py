#!/usr/bin/env python
"""Вселенная перп-контрактов Binance НА ДАТУ — для стратегий, которые выбирают состав сами.

Зачем не `fetch_tickers`: тот отдаёт сегодняшних лидеров, и в списке оказываются
токенизированные акции, золото и монеты, которых в проверяемом периоде не существовало.
А умерших — LUNA, FTT — в нём нет вовсе, и любая стратегия «покупать лучших» показывает
прибыль всегда.

Здесь список берётся из каталога архива и фильтруется по НАЛИЧИЮ МЕСЯЧНОГО ФАЙЛА на
указанную дату: существовал контракт тогда — попал во вселенную, умер позже — остался.

    python scripts/perp_universe.py --month 2021-06 --out /app/data/universe-perps.txt
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import httpx

LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
PREFIX = "data/futures/um/monthly/klines/"
ARCHIVE = "https://data.binance.vision/data/futures/um/monthly/klines"

# Токенизированные акции, металлы и стейблы: к крипте отношения не имеют, а в топ
# по обороту 2026 года лезут первыми.
NOT_CRYPTO = {
    "XAU", "XAG", "SNDK", "SOXL", "SKHYNIX", "SKHY", "MU", "SPCX", "KORU", "SNXX",
    "QQQ", "MSTR", "TSLA", "INTC", "CRCL", "MUB", "BZ", "CL", "USDC", "FDUSD",
    "TUSD", "BUSD", "DAI", "USDP", "EUR", "USD1", "USDE", "XUSD",
}


def all_symbols(client: httpx.Client) -> list[str]:
    out: list[str] = []
    marker: str | None = None
    for _ in range(40):
        params = {"delimiter": "/", "prefix": PREFIX}
        if marker:
            params["marker"] = marker
        r = client.get(LISTING, params=params)
        r.raise_for_status()
        found = re.findall(rf"<Prefix>{PREFIX}([^/]+)/</Prefix>", r.text)
        if not found:
            break
        out += found
        if "<IsTruncated>true</IsTruncated>" not in r.text:
            break
        marker = PREFIX + found[-1] + "/"
    return sorted(set(out))


def ccxt_name(symbol: str) -> str:
    return f"{symbol[: -len('USDT')]}/USDT:USDT"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--month", default="2021-06", help="месяц, на который проверяется наличие")
    ap.add_argument("--out", help="куда записать список (по умолчанию только печать)")
    ap.add_argument("--tf", default="1d")
    args = ap.parse_args()

    with httpx.Client(timeout=60) as client:
        symbols = [
            s
            for s in all_symbols(client)
            if s.endswith("USDT") and "_" not in s and s[: -len("USDT")] not in NOT_CRYPTO
        ]
        print(f"перпов в архиве: {len(symbols)}", flush=True)
        alive: list[str] = []
        for i, s in enumerate(symbols, 1):
            url = f"{ARCHIVE}/{s}/{args.tf}/{s}-{args.tf}-{args.month}.zip"
            try:
                if client.head(url, timeout=20).status_code == 200:
                    alive.append(s)
            except httpx.HTTPError:
                continue
            if i % 100 == 0:
                print(f"  проверено {i}/{len(symbols)}, живых {len(alive)}", flush=True)

    names = [ccxt_name(s) for s in alive]
    print(f"существовали в {args.month}: {len(names)}")
    if args.out:
        Path(args.out).write_text("\n".join(names), encoding="utf-8")
        print(f"записано в {args.out}")
    else:
        print(",".join(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
