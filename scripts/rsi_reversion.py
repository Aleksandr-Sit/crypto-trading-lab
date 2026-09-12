#!/usr/bin/env python
"""Возврат к среднему по RSI-2 (кандидат C5): переносится ли правило Коннорса в крипту.

Классика краткосрочной торговли акциями: покупать перепроданность (RSI с периодом 2 ниже
десяти) внутри восходящего тренда (цена выше двухсотдневной средней), выходить при первом
же отскоке. Причина в акциях — принудительные продажи и маржин-коллы, которые толкают цену
ниже справедливой на день-два.

Главный контроль здесь один, и без него замер бессмыслен: **сравнивать надо с обычным днём
ВНУТРИ ТОГО ЖЕ ТРЕНДА.** Правило состоит из двух частей — фильтра тренда и сигнала
перепроданности. Сравнение с произвольным днём измерит первую часть: «покупать выше
двухсотдневной» в крипте работает само по себе, и приписать её заслугу RSI значит
обмануть себя.

Наблюдение — ДАТА, а не бар: восемь монет в один день падают вместе.

    python scripts/rsi_reversion.py --root /app/data
    python scripts/rsi_reversion.py --threshold 5 --horizons 1,3,5,10
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date
from math import sqrt
from pathlib import Path
from statistics import fmean, stdev

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lab.data.store import CandleStore  # noqa: E402

DEFAULT_BASES = "BTC,ETH,SOL,XRP,DOGE,AVAX,LINK,ADA,BNB,LTC"
TAKER_ROUND = 0.10  # круг по тейкеру, %


def rsi(closes: list[float], period: int) -> list[float | None]:
    """RSI по Уайлдеру: сглаженные средние роста и падения."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    avg_g = fmean(gains[:period])
    avg_l = fmean(losses[:period])
    for i in range(period, len(closes)):
        if i > period:
            avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
            avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


class Daily:
    """Наблюдения по датам: монеты внутри дня — одно наблюдение, а не десять."""

    __slots__ = ("by_date",)

    def __init__(self) -> None:
        self.by_date: dict[date, list[float]] = defaultdict(lambda: [0.0, 0.0])

    def add(self, d: date, x: float) -> None:
        cell = self.by_date[d]
        cell[0] += x
        cell[1] += 1

    def means(self) -> list[float]:
        return [t / n for t, n in self.by_date.values()]

    @property
    def days(self) -> int:
        return len(self.by_date)

    @property
    def mean(self) -> float:
        vals = self.means()
        return fmean(vals) if vals else 0.0

    @property
    def se(self) -> float:
        vals = self.means()
        return stdev(vals) / sqrt(len(vals)) if len(vals) > 1 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bases", default=DEFAULT_BASES)
    ap.add_argument("--period", type=int, default=2, help="период RSI")
    ap.add_argument("--threshold", type=float, default=10.0, help="порог перепроданности")
    ap.add_argument("--trend", type=int, default=200, help="средняя для фильтра тренда")
    ap.add_argument("--horizons", default="1,3,5")
    ap.add_argument("--from-year", type=int, default=2019)
    ap.add_argument("--root", default="data")
    args = ap.parse_args()

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    cs = CandleStore(args.root)
    longest = max(horizons)

    # Три группы: сигнал в тренде, обычный день В ТРЕНДЕ (контроль) и обычный день вообще.
    hit: dict[int, Daily] = defaultdict(Daily)
    trend: dict[int, Daily] = defaultdict(Daily)
    every: dict[int, Daily] = defaultdict(Daily)
    yearly: dict[int, tuple[Daily, Daily]] = defaultdict(lambda: (Daily(), Daily()))
    events = 0

    for base in [b.strip() for b in args.bases.split(",") if b.strip()]:
        rows = cs.query(
            "select ts, close::DOUBLE as close from {candles} order by ts",
            "binance",
            f"{base}/USDT",
            "1d",
        )
        closes = [float(r["close"]) for r in rows if r["close"]]
        stamps = [r["ts"] for r in rows if r["close"]]
        if len(closes) < args.trend + longest + 5:
            print(f"{base}: ряда мало ({len(closes)})")
            continue
        values = rsi(closes, args.period)
        for i in range(args.trend, len(closes) - longest):
            ts = stamps[i]
            if ts.year < args.from_year:
                continue
            sma = fmean(closes[i - args.trend + 1 : i + 1])
            above = closes[i] > sma
            r = values[i]
            d = ts.date()
            oversold = r is not None and r < args.threshold and above
            if oversold:
                events += 1
            for h in horizons:
                ret = (closes[i + h] / closes[i] - 1) * 100
                every[h].add(d, ret)
                if above:
                    (hit[h] if oversold else trend[h]).add(d, ret)
            mid = horizons[len(horizons) // 2]
            ret_mid = (closes[i + mid] / closes[i] - 1) * 100
            if above:
                a, b = yearly[ts.year]
                (a if oversold else b).add(d, ret_mid)
        print(f"{base}: {len(closes)} дней")

    if not events:
        print("\nсобытий не нашлось")
        return 0
    rule = f"RSI{args.period} < {args.threshold} при цене выше SMA{args.trend}"
    print(f"\nсобытий ({rule}): {events}")
    cols = f"{'гор.':6}{'перепроданность':>17}{'обычный в тренде':>19}{'разница':>10}"
    print("\n" + cols + f"{'шум (2σ)':>11}   вердикт")
    for h in horizons:
        diff = hit[h].mean - trend[h].mean
        se = sqrt(hit[h].se ** 2 + trend[h].se ** 2)
        if abs(diff) < 2 * se:
            note = "в пределах шума"
        elif abs(diff) < TAKER_ROUND:
            note = "меньше издержек"
        else:
            note = "ПЕРЕЖИВАЕТ ОБА ПОРОГА"
        print(
            f"{h:<6}{hit[h].mean:>16.2f}%{trend[h].mean:>18.2f}%"
            f"{diff:>10.2f}{2 * se:>11.2f}   {note}"
        )
    plain = every[horizons[0]].mean
    print(f"\nдля справки, обычный день БЕЗ фильтра тренда, {horizons[0]} дн: {plain:.2f}%")
    print("  (разница с колонкой «обычный в тренде» — это заслуга фильтра, а не RSI)")

    print(f"\nПО ГОДАМ, горизонт {horizons[len(horizons) // 2]} дн")
    print(f"{'год':7}{'дат':>7}{'перепродан':>13}{'обычный в тренде':>19}{'разница':>10}")
    signs: list[float] = []
    for year in sorted(yearly):
        a, b = yearly[year]
        if not a.days or not b.days:
            continue
        signs.append(a.mean - b.mean)
        print(f"{year:<7}{a.days:>7}{a.mean:>12.2f}%{b.mean:>18.2f}%{a.mean - b.mean:>10.2f}")
    if len(signs) > 1:
        pos = sum(1 for s in signs if s > 0)
        print(f"знак совпадает в {max(pos, len(signs) - pos)} годах из {len(signs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
