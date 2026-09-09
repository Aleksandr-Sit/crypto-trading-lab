#!/usr/bin/env python
"""Чем определять фазу «альты в тренде» — проверка кандидатов на собранных данных.

Зачем: кросс-моментум альтов даёт до +41.7% годовых в свою фазу и ровно теряет вне её,
поэтому вся его ценность в том, умеем ли мы эту фазу опознать. Фильтр из карточки
(`BTC > SMA(100)`) не годится — он опоздал и на входе в фазу, и на выходе: в марте 2020
вывел в кэш уже после обвала, а 24.09.2020, в день пробоя стопа, разрешал покупки.

Считаются три кандидата по дневным свечам вселенной (647 пар):

  breadth_sma50   — доля пар вселенной выше своей SMA(50), %
  breadth_high20  — доля пар, обновивших максимум за 20 дней, %
  alt_share       — доля оборота альтов в общем обороте (альты + BTC), %

Скрипт НИЧЕГО не решает: он печатает ряд, чтобы его можно было сопоставить с помесячным
результатом стратегии. Вывод «этот индикатор годится» делается не здесь.

    python scripts/market_breadth.py --from 2020-01 --to 2021-06
    python scripts/market_breadth.py --csv breadth.csv
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data import CandleStore  # noqa: E402

D = Decimal
SMA_DAYS = 50
HIGH_DAYS = 20
BTC = "BTC/USDT"


def _month(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m").replace(tzinfo=UTC)


def universe(root: str) -> list[str]:
    """Спотовые пары с дневными свечами: имя каталога `BTC_USDT` → `BTC/USDT`."""
    base = Path(root) / "candles" / "venue=binance"
    out = []
    for d in sorted(base.glob("instrument=*")):
        name = d.name.split("=", 1)[1]
        if name.count("_") != 1 or not (d / "tf=1d").exists():
            continue
        out.append(name.replace("_", "/"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="data")
    ap.add_argument("--from", dest="since", default="2019-11")
    ap.add_argument("--to", dest="upto", default="2026-09")
    ap.add_argument("--csv", help="куда сохранить ряд (по умолчанию только печать)")
    args = ap.parse_args()

    store = CandleStore(args.root)
    pairs = universe(args.root)
    a, b = _month(args.since), _month(args.upto)
    # Запас слева: SMA(50) на первую дату окна считается по данным ДО него.
    read_from = a - timedelta(days=SMA_DAYS + HIGH_DAYS + 5)
    print(f"вселенная: {len(pairs)} пар, окно {a:%m.%Y}–{b:%m.%Y}", flush=True)

    above = defaultdict(int)  # день → сколько пар выше своей SMA(50)
    highs = defaultdict(int)  # день → сколько пар обновили максимум 20 дней
    total = defaultdict(int)  # день → сколько пар вообще торговалось
    alt_turnover: dict[datetime, Decimal] = defaultdict(lambda: D(0))
    btc_turnover: dict[datetime, Decimal] = defaultdict(lambda: D(0))

    for i, pair in enumerate(pairs, 1):
        rows = store.read("binance", pair, "1d", read_from, b)
        if len(rows) < SMA_DAYS + 1:
            continue
        closes = [c.close for c in rows]
        for k in range(SMA_DAYS, len(rows)):
            day = rows[k].ts
            if day < a:
                continue
            total[day] += 1
            sma = sum(closes[k - SMA_DAYS : k], D(0)) / SMA_DAYS
            if closes[k] > sma:
                above[day] += 1
            window = closes[max(0, k - HIGH_DAYS) : k]
            if window and closes[k] > max(window):
                highs[day] += 1
            turnover = closes[k] * rows[k].volume
            if pair == BTC:
                btc_turnover[day] += turnover
            else:
                alt_turnover[day] += turnover
        if i % 100 == 0:
            print(f"  обработано {i}/{len(pairs)} пар", flush=True)

    days = sorted(total)
    lines = ["дата,пар,breadth_sma50,breadth_high20,alt_share"]
    for day in days:
        n = total[day]
        if n < 20:  # слишком мало пар — доля ни о чём не говорит
            continue
        turn = alt_turnover[day] + btc_turnover[day]
        alt_share = float(alt_turnover[day] / turn * 100) if turn > 0 else 0.0
        lines.append(
            f"{day:%Y-%m-%d},{n},{above[day] * 100 / n:.1f},"
            f"{highs[day] * 100 / n:.1f},{alt_share:.1f}"
        )

    # Помесячная сводка — с ней ряд читается глазами, а посуточный лежит в csv.
    by_month: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for line in lines[1:]:
        date, _n, sma, high, share = line.split(",")
        by_month[date[:7]].append((float(sma), float(high), float(share)))
    print(f"\n{'месяц':8} {'выше SMA50':>11} {'новых максимумов':>18} {'доля альтов':>13}")
    for month in sorted(by_month):
        rows_m = by_month[month]
        avg = [sum(x[i] for x in rows_m) / len(rows_m) for i in range(3)]
        print(f"{month:8} {avg[0]:>10.1f}% {avg[1]:>17.1f}% {avg[2]:>12.1f}%")

    if args.csv:
        Path(args.csv).write_text("\n".join(lines), encoding="utf-8")
        print(f"\nпосуточный ряд: {args.csv} ({len(lines) - 1} строк)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
