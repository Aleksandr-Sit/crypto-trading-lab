#!/usr/bin/env python
"""Дневные ряды мировых активов в хранилище лаборатории (валюты, металлы, индексы, нефть).

Зачем: гипотеза владельца — «рынок менее волатильный, стратегий и трейдеров больше,
значит торговать по книжкам легче». Проверять её надо теми же инструментами, что и крипту,
а для этого ряды должны лежать в том же хранилище и звучать на том же языке.

Источник — дневные свечи Yahoo Finance: 15 лет по валютам, золоту, индексам и нефти,
без ключа. Площадка в хранилище зовётся `yahoo`, чтобы не путать с биржевыми рядами
и чтобы модель издержек не подставляла им биржевой тариф: у CFD издержки другие
(спред плюс плата за перенос через ночь), и это отдельный разговор.

**Что важно понимать про эти ряды.** Это цены БАЗОВОГО актива, а не CFD. Торгуют CFD,
и разница существенна: у контракта свой спред и ежедневная плата за перенос позиции,
которой у базового актива нет вовсе. Ряды годятся, чтобы проверить наличие эффекта;
вопрос «переживёт ли он издержки» решается отдельно — `scripts/tradfi_costs.py`.

    python scripts/tradfi_import.py --root /app/data
    python scripts/tradfi_import.py --symbols "EURUSD=X,GC=F" --years 20 --root /app/data
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.contracts import Candle  # noqa: E402
from lab.data.store import CandleStore  # noqa: E402

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# Хранилище держит деньги как decimal128(30, 12); у Yahoo цены приходят float'ом
# с шестнадцатью знаками, и Parquet отказывается их ужимать («Rescaling Decimal value
# would cause data loss»). Округляем явно — двенадцати знаков хватает любому активу.
_Q = Decimal("0.000000000001")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"

# Тикер Yahoo → имя в хранилище. Имена без слэшей и двоеточий: так их не спутать
# с биржевыми парами, а `_safe` в хранилище не будет их переписывать.
SYMBOLS = {
    "EURUSD=X": "EURUSD",
    "USDJPY=X": "USDJPY",
    "GBPUSD=X": "GBPUSD",
    "AUDUSD=X": "AUDUSD",
    "USDCHF=X": "USDCHF",
    "USDCAD=X": "USDCAD",
    "GC=F": "XAUUSD",
    "SI=F": "XAGUSD",
    "CL=F": "WTI",
    "NG=F": "NATGAS",
    "^GSPC": "SPX",
    "^NDX": "NDX",
    "^DJI": "DJI",
    "^GDAXI": "DAX",
    "^N225": "NIKKEI",
    "^FTSE": "FTSE",
}


def _money(value: float | int) -> Decimal:
    return Decimal(str(value)).quantize(_Q, rounding=ROUND_HALF_UP)


def fetch(client, symbol: str, years: int) -> list[dict]:
    r = client.get(
        CHART.format(symbol=symbol),
        params={"range": f"{years}y", "interval": "1d"},
        headers={"User-Agent": UA},
    )
    r.raise_for_status()
    result = (r.json().get("chart") or {}).get("result") or []
    if not result:
        return []
    block = result[0]
    stamps = block.get("timestamp") or []
    quote = (block.get("indicators") or {}).get("quote") or [{}]
    q = quote[0]
    rows = []
    for i, ts in enumerate(stamps):
        o, h, low, c = q.get("open"), q.get("high"), q.get("low"), q.get("close")
        vals = [o[i], h[i], low[i], c[i]]
        if any(v is None for v in vals):
            continue  # выходной или день без торгов — пропуск честнее выдуманной цены
        rows.append(
            {
                "ts": ts,
                "open": vals[0],
                "high": vals[1],
                "low": vals[2],
                "close": vals[3],
                "volume": (q.get("volume") or [0] * len(stamps))[i] or 0,
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="", help="тикеры Yahoo через запятую")
    ap.add_argument("--years", type=int, default=20)
    ap.add_argument("--pause", type=float, default=1.0)
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    import httpx

    wanted = (
        {s.strip(): SYMBOLS.get(s.strip(), s.strip().replace("=", "").replace("^", ""))
         for s in args.symbols.split(",") if s.strip()}
        if args.symbols
        else SYMBOLS
    )
    store = CandleStore(args.root)
    print(f"{'актив':12}{'тикер':12}{'дней':>8}{'период':>26}")
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for symbol, name in wanted.items():
            try:
                rows = fetch(client, symbol, args.years)
            except Exception as err:  # noqa: BLE001 — один тикер не роняет прогон
                print(f"{name:12}{symbol:12}   {type(err).__name__}")
                continue
            if not rows:
                print(f"{name:12}{symbol:12}   пусто")
                continue
            candles = [
                Candle(
                    instrument=name,
                    tf="1d",
                    ts=datetime.fromtimestamp(r["ts"], UTC).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ),
                    open=_money(r["open"]),
                    high=_money(r["high"]),
                    low=_money(r["low"]),
                    close=_money(r["close"]),
                    volume=_money(r["volume"]),
                )
                for r in rows
            ]
            store.write("yahoo", name, "1d", candles)
            first = candles[0].ts.date()
            last = candles[-1].ts.date()
            print(f"{name:12}{symbol:12}{len(candles):>8}   {first} … {last}")
            time.sleep(args.pause)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
