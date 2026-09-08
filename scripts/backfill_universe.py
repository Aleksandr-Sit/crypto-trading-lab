#!/usr/bin/env python
"""Свечи ВСЕЙ вселенной USDT-пар Binance, включая делистнутые.

Зачем отдельный скрипт, а не `lab data backfill --symbols …`: список пар нельзя брать
у работающей биржи. `fetch_tickers` отдаёт только то, что торгуется СЕГОДНЯ, и бэктест
на таком списке покупает исключительно выживших — LUNA, FTT и SRM в нём нет, а они были
в топе по обороту ровно перед тем, как обнулиться. Кросс-секционные стратегии на этом
показывают прибыль всегда, и она поддельная.

Источник списка — каталог архивов data.binance.vision: там лежат и мёртвые пары.

    python scripts/backfill_universe.py --dry-run          # показать вселенную и выйти
    python scripts/backfill_universe.py --tf 1d --days 2500

Повторный запуск дешёвый: уже скачанные ряды пропускаются (`уже загружено, пропуск`).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data import CandleStore  # noqa: E402
from lab.data.backfill_cex import backfill_venue  # noqa: E402

LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
PREFIX = "data/spot/monthly/klines/"

# Стейблы: пара USDC/USDT никуда не движется, моментум по ней — шум округления.
STABLE = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "SUSD", "USDS", "USDE", "PYUSD",
    "RLUSD", "AEUR", "EUR", "GBP", "TRY", "BRL", "ARS", "JPY", "RUB", "UAH", "ZAR",
    "IDRT", "NGN", "BIDR", "VAI", "USTC", "PAX", "USD1", "XUSD",
}
# Токенизированные акции Binance (2025+): к крипте отношения не имеют, а в топ по обороту
# попадают. Суффикс B неотличим от обычных тикеров, поэтому список явный.
STOCKS = {
    "SNDKB", "CRCLB", "SPCXB", "QQQB", "MSTRB", "TSLAB", "SOXLB", "INTCB", "KORUB",
    "MUB", "SNXXB", "NVDAB", "AAPLB", "METAB", "GOOGLB", "AMZNB", "COINB", "HOODB",
}
LEVERAGED = ("UP", "DOWN", "BULL", "BEAR")


def archive_symbols(client: httpx.Client) -> list[str]:
    """Все символы, у которых КОГДА-ЛИБО были архивы спотовых свечей."""
    out: list[str] = []
    marker: str | None = None
    for _ in range(50):
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
    return out


def universe(symbols: list[str]) -> list[str]:
    """Символы вида `BTCUSDT` → пары ccxt `BTC/USDT`, без стейблов, плечевых и акций."""
    pairs: list[str] = []
    for sym in sorted(set(symbols)):
        if not sym.endswith("USDT"):
            continue
        base = sym[: -len("USDT")]
        if not base or base in STABLE or base in STOCKS:
            continue
        if base.endswith(LEVERAGED):
            continue
        pairs.append(f"{base}/USDT")
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--days", type=int, default=2500)
    ap.add_argument("--root", default="data")
    ap.add_argument("--batch", type=int, default=20, help="пар за один вызов бэкфилла")
    ap.add_argument("--limit", type=int, default=0, help="взять только первые N пар")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with httpx.Client(timeout=60) as client:
        pairs = universe(archive_symbols(client))
    if args.limit:
        pairs = pairs[: args.limit]
    print(f"вселенная: {len(pairs)} пар к USDT (включая делистнутые)", flush=True)
    if args.dry_run:
        print(", ".join(pairs))
        return 0

    store = CandleStore(args.root)
    started, errors = time.monotonic(), 0
    for i in range(0, len(pairs), args.batch):
        batch = pairs[i : i + args.batch]
        results = backfill_venue(store, "binance", batch, args.tf, days=args.days)
        for r in results:
            if r.error:
                errors += 1
                print(f"  ошибка {r.instrument}: {r.error}", flush=True)
        done = min(i + args.batch, len(pairs))
        speed = (time.monotonic() - started) / done
        left = int(speed * (len(pairs) - done) / 60)
        print(
            f"{done}/{len(pairs)} пар · {speed:.1f} с/пара · осталось ~{left} мин"
            f" · ошибок {errors}",
            flush=True,
        )
    print(f"готово: {len(pairs)} пар, ошибок {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
