#!/usr/bin/env python
"""Эффект листинга (D1): что происходит с монетой после первого дня торгов на Binance.

Литература (Blockchain Research Lab, 327 листингов): +14.7% накопленной аномальной
доходности К дню листинга и отрицательная доходность ПОСЛЕ. Свежие данные говорят
об обратном знаке ещё жёстче: из листингов Binance 2025 года в плюсе осталось 11%.
Оба варианта торгуемы — покупать нельзя, но можно не покупать или шортить перп.

Дата листинга берётся из архива: первая дневная свеча ряда. Вселенная из архива
листингов содержит и умершие пары — иначе выживших было бы больше, чем есть.

Что считается:

* доходность от закрытия ПЕРВОГО полного дня (день листинга сам не торгуем — цена
  открытия там условна) на горизонты 3, 7, 30, 90 дней;
* то же у BTC за те же дни — ориентир обязателен: листинги идут волнами в бычьи
  фазы, и без него «эффект листинга» окажется эффектом рынка;
* **наблюдение — листинг, но шум — по МЕСЯЦАМ**: листинги одной недели идут в один
  рынок и не независимы;
* разбивка по годам — эффект менял знак, и это надо увидеть, а не усреднить.

    python scripts/listing_effect.py --root /app/data
    python scripts/listing_effect.py --from-year 2023 --root /app/data
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from math import sqrt
from pathlib import Path
from statistics import fmean, median, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

HORIZONS = (3, 7, 30, 90)


def first_days(cs: CandleStore, name: str, need: int) -> list[tuple[date, float]]:
    """Первые `need` дневных закрытий ряда."""
    rows = cs.query(
        f"select ts, close::DOUBLE as c from {{candles}} order by ts limit {need}",
        "binance",
        name,
        "1d",
    )
    return [(r["ts"].date(), float(r["c"])) for r in rows if r["c"] and r["c"] > 0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--universe", default="universe-1d.txt")
    ap.add_argument("--from-year", type=int, default=2021)
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    root = Path(args.root)
    cs = CandleStore(root)
    names = [ln.strip() for ln in (root / args.universe).read_text().splitlines() if ln.strip()]
    btc = {
        r["ts"].date(): float(r["c"])
        for r in cs.query(
            "select ts, close::DOUBLE as c from {candles} order by ts", "binance", "BTC/USDT", "1d"
        )
    }
    longest = max(HORIZONS)

    # По месяцам листинга: несколько листингов одного месяца — одно наблюдение.
    by_month: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    by_year: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    listings = 0
    skipped_btc = 0
    for name in names:
        days = first_days(cs, name, longest + 3)
        if len(days) < longest + 2:
            continue
        d0, p0 = days[1]  # закрытие первого ПОЛНОГО дня
        if d0.year < args.from_year:
            continue
        if d0 not in btc:
            skipped_btc += 1
            continue
        listings += 1
        key = d0.year * 100 + d0.month
        for h in HORIZONS:
            d1, p1 = days[1 + h]
            coin = (p1 / p0 - 1) * 100
            b1 = btc.get(d1)
            if b1 is None:
                continue
            excess = coin - (b1 / btc[d0] - 1) * 100
            by_month[h][key].append(excess)
            by_year[h][d0.year].append(excess)

    print(f"листингов с {args.from_year}: {listings} (без ориентира BTC: {skipped_btc})")
    print(f"\n{'горизонт':10}{'сверх BTC, средняя':>20}{'медиана':>10}{'шум (2σ по мес.)':>18}"
          f"{'доля в плюсе':>14}{'месяцев':>9}")
    for h in HORIZONS:
        months = by_month[h]
        if not months:
            continue
        per_month = [fmean(v) for v in months.values()]
        all_vals = [x for v in months.values() for x in v]
        se = stdev(per_month) / sqrt(len(per_month)) if len(per_month) > 1 else 0.0
        up = sum(1 for x in all_vals if x > 0) / len(all_vals) * 100
        print(
            f"{h:>4} дн   {fmean(all_vals):>19.2f}%{median(all_vals):>9.2f}%"
            f"{2 * se:>18.2f}{up:>13.0f}%{len(per_month):>9}"
        )

    print(f"\nПО ГОДАМ, сверх BTC, горизонт 30 дн")
    print(f"{'год':7}{'листингов':>11}{'средняя':>10}{'медиана':>10}{'в плюсе':>9}")
    for year in sorted(by_year[30]):
        vals = by_year[30][year]
        up = sum(1 for x in vals if x > 0) / len(vals) * 100
        print(f"{year:<7}{len(vals):>11}{fmean(vals):>9.1f}%{median(vals):>9.1f}%{up:>8.0f}%")
    print(
        "\nЧитать так: медиана важнее средней — один листинг с +2000% тянет среднюю,\n"
        "а купить его заранее было нельзя. Отрицательная медиана при доле в плюсе\n"
        "ниже 40% — это торгуемо в обратную сторону, если есть перп для шорта."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
